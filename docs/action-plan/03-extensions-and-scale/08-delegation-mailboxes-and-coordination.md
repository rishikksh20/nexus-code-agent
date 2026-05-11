# Chapter 8: Delegation, Mailboxes, And Coordination

## Objective

Add a multi-agent layer carefully, without pretending that more agents automatically means better results. This chapter combines the strongest advanced coordination ideas from both tutorial families:

- coordinator-worker delegation from `openai-code-tutorial`
- explicit mailbox communication
- collaboration and concurrency ideas from the second edition of `agentic-framework-tutorial`

## When To Add Delegation

Only add delegation after the single-agent harness already has:

- typed messages and tool results
- session persistence
- permission controls
- structured logging
- a testing strategy

If the single-agent runtime is still unstable, workers will amplify the confusion.

## Add A Coordinator Model

The coordinator owns planning and task assignment. Workers own bounded execution.

```python
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(Enum):
    REQUESTED = "requested"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    title: str
    instructions: str
    status: TaskStatus = TaskStatus.REQUESTED
    assigned_worker: str | None = None
    result_summary: str | None = None
```

Keep delegation explicit. A worker should never appear out of nowhere because the model happened to mention one.

## Add Typed Mailboxes

The OpenAI tutorial improves on vague coordination by making message passing explicit. Keep that improvement.

```python
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum


class MessageKind(Enum):
    COMMAND = "command"
    STATUS = "status"
    RESULT = "result"
    PERMISSION = "permission"
    APPROVAL = "approval"


@dataclass(slots=True)  # NOT frozen: payload is a dict and can be built incrementally
class AgentMessage:
    sender: str
    recipient: str
    kind: MessageKind
    payload: dict


class InMemoryMailbox:
    def __init__(self) -> None:
        self._queues: dict[str, deque[AgentMessage]] = defaultdict(deque)

    def send(self, message: AgentMessage) -> None:
        self._queues[message.recipient].append(message)

    def receive_all(self, recipient: str) -> list[AgentMessage]:
        queue = self._queues[recipient]
        items = list(queue)
        queue.clear()
        return items
```

This keeps communication inspectable, testable, and replayable.

## Worker Lifecycle

Define the worker loop as a bounded task processor.

```python
class WorkerAgent:
    def __init__(self, worker_id: str, mailbox: InMemoryMailbox) -> None:
        self.worker_id = worker_id
        self.mailbox = mailbox

    async def step(self) -> None:
        for message in self.mailbox.receive_all(self.worker_id):
            if message.kind is MessageKind.COMMAND:
                result = {"status": "completed", "summary": f"Finished {message.payload['task_id']}"}
                self.mailbox.send(
                    AgentMessage(
                        sender=self.worker_id,
                        recipient=message.sender,
                        kind=MessageKind.RESULT,
                        payload=result,
                    )
                )
```

The worker does not own global state. It owns the current task and communicates through the mailbox.

In the current Nexus implementation, the worker lifecycle now goes one step further than the minimal example:

- the coordinator still owns `TaskRecord` state and mailbox routing
- each worker runs a restricted inner `Agent` loop for its assigned task instead of returning a hard-coded result
- worker approvals still route back through the coordinator before mutating work can continue
- task-level tool scope is explicit through per-task allowlists

## Spawn Workers As AsyncIO Tasks

A worker is a long-running coroutine. Use `asyncio.create_task()` to start it alongside the coordinator, not as a blocking call.

```python
import asyncio


async def run_coordinator(mailbox: InMemoryMailbox) -> None:
    worker1 = WorkerAgent("worker-1", mailbox)
    worker2 = WorkerAgent("worker-2", mailbox)

    # Start workers as background tasks
    tasks = [
        asyncio.create_task(run_worker_loop(worker1)),
        asyncio.create_task(run_worker_loop(worker2)),
    ]

    # Coordinator sends a command
    mailbox.send(AgentMessage(
        sender="coordinator",
        recipient="worker-1",
        kind=MessageKind.COMMAND,
        payload={"task_id": "t-001"},
    ))

    # Give workers time to process; in production use event-driven wakeups
    await asyncio.sleep(0.1)

    # Cancel workers when done
    for task in tasks:
        task.cancel()


async def run_worker_loop(worker: WorkerAgent, poll_interval: float = 0.05) -> None:
    while True:
        await worker.step()
        await asyncio.sleep(poll_interval)
```

In a production setup, replace the `asyncio.sleep` polling with an `asyncio.Event` or `asyncio.Queue` so workers wake up only when a message is available.

## Route Permissions Through The Coordinator

This is where multi-agent systems often go wrong. If every worker can independently approve its own risky actions, your policy model is broken.

Use a simple rule:

- workers can request permission
- the coordinator or top-level user-facing runtime decides
- granted permissions should be narrow and auditable

That can look like this:

```python
permission_request = AgentMessage(
    sender="worker-1",
    recipient="coordinator",
    kind=MessageKind.PERMISSION,
    payload={"tool": "write_note", "reason": "Need to persist findings"},
)
```

In the current runtime this is wired through the same confirmation model used by the single-agent loop. A delegated worker can surface an approval request, but only the coordinator resolves it. Workers do not auto-approve their own mutating actions.

## Add File Coordination If Workers Touch Shared State

The second-edition material highlights an important scaling reality: concurrent work creates coordination problems before it creates intelligence gains.

If workers operate on shared files or shared sessions, add at least one of these:

- soft file locks
- optimistic concurrency checks with version numbers
- ownership windows for specific paths or tasks

For a minimal harness, optimistic concurrency is usually enough.

## Action Plan

1. Add a coordinator that explicitly creates `TaskRecord` items.
2. Add a typed mailbox implementation.
3. Make workers poll their mailbox and emit status and result messages.
4. Route risky actions back through coordinator approval.
5. Add a minimal concurrency strategy for shared files or session state.
6. Log sender, recipient, task ID, and decision ID on every cross-agent message.

## Current Runtime Notes

The current Nexus runtime now supports the following coordination surface in practice:

- `/delegate spawn ...` creates a coordinator-owned task record and dispatches a typed mailbox command
- repeated `--tool` flags restrict the delegated worker to a bounded registry for that task
- `/delegate approvals`, `/delegate approve <decision_id>`, and `/delegate reject <decision_id>` keep approval decisions centralized
- optimistic resource-version checks prevent two delegated tasks from silently committing the same claimed resource

Current limitation:

- if a delegated worker encounters a clarification requirement, it reports that back as a task outcome instead of supporting a full interactive clarification round-trip yet

## Validation Checklist

- A coordinator can assign a task to a worker and receive a typed result back.
- Workers do not mutate global state without explicit routing.
- Permission decisions remain centralized.
- Mailbox messages are inspectable and replayable.
- Concurrent tasks have at least one conflict-avoidance strategy.

## Definition Of Done

This chapter is complete when the multi-agent system is more explainable than magical. If you cannot trace why a worker acted, the coordination model is still too implicit.