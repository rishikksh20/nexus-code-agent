# 11 — Agent Communication: Mailboxes and Typed Messages

## Prerequisites

Complete [10-swarms-and-delegation.md](10-swarms-and-delegation.md) first.

Chapter 10 made workers fire-and-forget — the coordinator spawns them and waits for a result. That is enough for simple batch tasks, but it breaks for anything that needs mid-task guidance:

- "Stop searching and focus on this new clue."
- "Permission to write files has been granted."
- "Cancel this worker — the user changed requirements."

This chapter adds a **mailbox system** — explicit, typed, inspectable message passing between the coordinator and its workers.

---

## What you will build

```
agent/
    mailbox.py      ← NEW: MessageKind, AgentMessage, Mailbox, InMemoryMailbox
    swarm.py        ← updated: workers check their mailbox each loop iteration
    tools.py        ← updated: SendWorkerMessageTool, CheckWorkerMailboxTool
```

---

## 1. Why mailboxes beat implicit communication

Without mailboxes, agent-to-agent communication tends to become:

```python
# Anti-patterns that feel simple but cause problems:
shared_dict["worker_abc"]["status"] = "done"    # shared mutable state
print(f"Worker {wid}: done")                     # parsing logs for state
callback_registry[wid]("done")                  # hidden callback chains
```

These work briefly, then become impossible to inspect, test, or debug.

A mailbox gives you:
- **Who** sent this message?
- **Who** should receive it?
- **What kind** of message is it?
- **When** was it sent?
- **What task** does it belong to?

---

## 2. Create `agent/mailbox.py`

```python
# agent/mailbox.py

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid


# ── Message kinds ─────────────────────────────────────────────────────────────

class MessageKind(str, Enum):
    """
    Typed categories for agent messages.

    Keeping these separate prevents the coordinator from confusing
    a status update with a permission request.
    """
    COMMAND     = "command"      # coordinator → worker: next instruction
    STATUS      = "status"       # worker → coordinator: progress update
    RESULT      = "result"       # worker → coordinator: final output
    CONTROL     = "control"      # runtime-level: cancel, shutdown, heartbeat
    PERMISSION  = "permission"   # worker → coordinator: requesting approval
    APPROVAL    = "approval"     # coordinator → worker: permission granted/denied


# ── Message ───────────────────────────────────────────────────────────────────

@dataclass
class AgentMessage:
    """
    One structured message between agents.

    sender          — who sent it (agent id, "coordinator", "user")
    recipient       — who should receive it (agent id, "coordinator")
    kind            — MessageKind — determines how it is processed
    payload         — structured data (never raw text blobs)
    task_id         — which task this message relates to
    correlation_id  — links a request to its response
    timestamp       — ISO 8601 creation time
    message_id      — unique identifier
    """
    sender: str
    recipient: str
    kind: MessageKind
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    correlation_id: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    # ── Convenience constructors ──────────────────────────────────────────────

    @classmethod
    def command(cls, *, sender: str, recipient: str, task_id: str, instruction: str) -> "AgentMessage":
        return cls(
            sender=sender, recipient=recipient,
            kind=MessageKind.COMMAND, task_id=task_id,
            payload={"instruction": instruction},
        )

    @classmethod
    def status(cls, *, sender: str, task_id: str, message: str, progress: int = 0) -> "AgentMessage":
        return cls(
            sender=sender, recipient="coordinator",
            kind=MessageKind.STATUS, task_id=task_id,
            payload={"message": message, "progress": progress},
        )

    @classmethod
    def result(cls, *, sender: str, task_id: str, content: str, success: bool = True) -> "AgentMessage":
        return cls(
            sender=sender, recipient="coordinator",
            kind=MessageKind.RESULT, task_id=task_id,
            payload={"content": content, "success": success},
        )

    @classmethod
    def permission_request(
        cls, *, sender: str, task_id: str, action: str, reason: str
    ) -> "AgentMessage":
        msg = cls(
            sender=sender, recipient="coordinator",
            kind=MessageKind.PERMISSION, task_id=task_id,
            payload={"action": action, "reason": reason},
        )
        msg.correlation_id = msg.message_id   # response must reference this id
        return msg

    @classmethod
    def approval_response(
        cls, *, sender: str, recipient: str, task_id: str,
        approved: bool, correlation_id: str
    ) -> "AgentMessage":
        return cls(
            sender=sender, recipient=recipient,
            kind=MessageKind.APPROVAL, task_id=task_id,
            correlation_id=correlation_id,
            payload={"approved": approved},
        )

    @classmethod
    def cancel(cls, *, sender: str, recipient: str, task_id: str, reason: str = "") -> "AgentMessage":
        return cls(
            sender=sender, recipient=recipient,
            kind=MessageKind.CONTROL, task_id=task_id,
            payload={"action": "cancel", "reason": reason},
        )


# ── Mailbox ───────────────────────────────────────────────────────────────────

class Mailbox:
    """Abstract mailbox interface."""

    async def send(self, message: AgentMessage) -> None:
        raise NotImplementedError

    async def receive_all(self, recipient: str) -> list[AgentMessage]:
        raise NotImplementedError

    async def receive_by_kind(
        self, recipient: str, kind: MessageKind
    ) -> list[AgentMessage]:
        all_msgs = await self.receive_all(recipient)
        return [m for m in all_msgs if m.kind == kind]


class InMemoryMailbox(Mailbox):
    """
    Simple in-process mailbox using asyncio queues.

    One queue per recipient id.
    Messages are consumed (popped) when received.
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[AgentMessage]] = {}

    def _queue_for(self, recipient: str) -> asyncio.Queue[AgentMessage]:
        if recipient not in self._queues:
            self._queues[recipient] = asyncio.Queue()
        return self._queues[recipient]

    async def send(self, message: AgentMessage) -> None:
        await self._queue_for(message.recipient).put(message)

    async def receive_all(self, recipient: str) -> list[AgentMessage]:
        """Drain all pending messages for this recipient (non-blocking)."""
        q = self._queue_for(recipient)
        messages = []
        while not q.empty():
            try:
                messages.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return messages

    async def wait_for_approval(
        self, recipient: str, correlation_id: str, timeout: float = 30.0
    ) -> bool | None:
        """
        Wait until an APPROVAL message arrives for the given correlation_id.
        Returns True/False for approved, or None on timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            msgs = await self.receive_by_kind(recipient, MessageKind.APPROVAL)
            for msg in msgs:
                if msg.correlation_id == correlation_id:
                    return msg.payload.get("approved", False)
            await asyncio.sleep(0.2)
        return None   # timed out
```

---

## 3. Update worker loop to check mailbox

Update `make_local_worker_fn` in `agent/swarm.py` to check for messages each iteration:

```python
# agent/swarm.py  — updated worker_fn

from agent.mailbox import InMemoryMailbox, MessageKind, AgentMessage

def make_local_worker_fn(model_client, tool_registry_factory, base_prompt, cwd, mailbox: InMemoryMailbox):

    async def worker_fn(task: TaskRecord) -> None:
        task.status = TaskStatus.RUNNING
        task.notes.append(f"Worker {task.worker_id} started.")

        registry = tool_registry_factory(task.allowed_tools)
        worker_agent = Agent(
            model_client=model_client,
            tool_registry=registry,
            base_prompt=(
                f"{base_prompt}\n\n"
                f"Role: {task.role}. Task: {task.description}.\n"
                f"Complete the task and stop."
            ),
            cwd=cwd,
            model_name=f"worker-{task.worker_id}",
            mode=ExecutionMode(task.mode),
        )

        output_parts = []

        try:
            async for event in worker_agent.run(task.prompt):
                # ── Check mailbox for control messages each iteration ─────────
                ctrl_msgs = await mailbox.receive_all(task.worker_id)
                for msg in ctrl_msgs:
                    if msg.kind == MessageKind.CONTROL and msg.payload.get("action") == "cancel":
                        task.fail(f"Cancelled: {msg.payload.get('reason', '')}")
                        return
                    elif msg.kind == MessageKind.COMMAND:
                        # Append follow-up instruction as a new user message
                        worker_agent.messages.append(
                            Message.user(msg.payload.get("instruction", ""))
                        )
                        task.notes.append(f"Received command: {msg.payload.get('instruction', '')[:60]}")

                # Emit progress
                if isinstance(event, AssistantTextDelta):
                    output_parts.append(event.text)
                elif isinstance(event, ToolExecutionCompleted):
                    task.notes.append(f"  {event.tool_name} → {event.output[:60]}")
                    # Send status update to coordinator
                    await mailbox.send(AgentMessage.status(
                        sender=task.worker_id,
                        task_id=task.task_id,
                        message=f"Used {event.tool_name}",
                    ))

            result = "\n".join(output_parts) or "(no output)"
            task.complete(result)
            await mailbox.send(AgentMessage.result(
                sender=task.worker_id,
                task_id=task.task_id,
                content=result,
            ))

        except Exception as exc:
            task.fail(str(exc))

    return worker_fn
```

---

## 4. Add communication tools to `agent/tools.py`

```python
# agent/tools.py  — add SendWorkerMessageTool and CheckWorkerMailboxTool

from agent.mailbox import InMemoryMailbox, AgentMessage, MessageKind


class SendWorkerMessageTool(BaseTool):
    """Send a follow-up instruction to a running worker."""
    name = "send_worker_message"
    description = (
        "Send a follow-up instruction or cancellation to a running worker. "
        "Use this to redirect a worker without spawning a new one."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "worker_id": {"type": "string", "description": "The worker_id to message."},
            "instruction": {"type": "string", "description": "The follow-up instruction."},
            "cancel": {"type": "boolean", "description": "If true, cancel the worker."},
        },
        "required": ["worker_id"],
    }
    is_mutating = False

    def __init__(self, mailbox: InMemoryMailbox, task_registry: SwarmBackend) -> None:
        self._mailbox = mailbox
        self._tasks = task_registry

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        worker_id = arguments.get("worker_id", "").strip()
        instruction = arguments.get("instruction", "").strip()
        cancel = arguments.get("cancel", False)

        if cancel:
            msg = AgentMessage.cancel(
                sender="coordinator", recipient=worker_id,
                task_id=worker_id, reason="Coordinator requested cancellation."
            )
        elif instruction:
            msg = AgentMessage.command(
                sender="coordinator", recipient=worker_id,
                task_id=worker_id, instruction=instruction,
            )
        else:
            return ToolResult(output="Error: provide 'instruction' or set 'cancel'=true.", is_error=True)

        await self._mailbox.send(msg)
        return ToolResult(output=f"Message sent to worker {worker_id}.")


class CheckWorkerMailboxTool(BaseTool):
    """Read pending messages from the coordinator's own mailbox (status updates from workers)."""
    name = "check_my_mailbox"
    description = "Read pending status messages from workers."
    input_schema = {"type": "object", "properties": {}, "required": []}
    is_mutating = False

    def __init__(self, mailbox: InMemoryMailbox) -> None:
        self._mailbox = mailbox

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        messages = await self._mailbox.receive_all("coordinator")
        if not messages:
            return ToolResult(output="No new messages.")
        lines = []
        for msg in messages:
            lines.append(
                f"[{msg.kind.value}] from={msg.sender} task={msg.task_id} "
                f"→ {str(msg.payload)[:80]}"
            )
        return ToolResult(output="\n".join(lines))
```

---

## 5. Update `main.py` to wire the mailbox

```python
# main.py  — updated build_agent()

from agent.mailbox import InMemoryMailbox

def build_agent(...) -> Agent:
    mailbox = InMemoryMailbox()
    backend = SwarmBackend()

    def worker_fn_factory():
        return make_local_worker_fn(
            model_client=client,
            tool_registry_factory=tool_registry_factory,
            base_prompt=DEFAULT_BASE_PROMPT,
            cwd=__import__("os").getcwd(),
            mailbox=mailbox,    # ← pass mailbox to workers
        )

    registry.register(SpawnWorkerTool(backend, worker_fn_factory))
    registry.register(GetWorkerResultTool(backend))
    registry.register(SendWorkerMessageTool(mailbox, backend))
    registry.register(CheckWorkerMailboxTool(mailbox))
    # ...rest unchanged...
```

---

## 6. See communication in action

With a real LLM coordinator:

```
you> spawn a researcher to list all Python files, then redirect it to only look at the tests
  · Thinking... (turn 1)
  ⚙ spawn_worker(description='list Python files', prompt='...', role='researcher', tools=['glob'])
  ✓ spawn_worker → Worker spawned. task_id=ab12cd34, role=researcher

  ⚙ send_worker_message(worker_id='ab12cd34', instruction='Only list files in ./tests/ directory')
  ✓ send_worker_message → Message sent to worker ab12cd34.

  ⚙ get_worker_result(task_id='ab12cd34', wait=true)
  ✓ get_worker_result → Status: completed
  Result:
  tests/test_auth.py
  tests/test_tools.py
  tests/conftest.py
```

Check coordinator's mailbox for status updates:

```
you> check for worker messages
  ⚙ check_my_mailbox()
  ✓ check_my_mailbox → [status] from=ab12cd34 task=ab12cd34 → {'message': 'Used glob'}
                        [result] from=ab12cd34 task=ab12cd34 → {'content': 'tests/test_auth.py...'}
```

---

## 7. Message kind reference

| Kind | Direction | Use case |
|---|---|---|
| `COMMAND` | Coordinator → Worker | Follow-up instruction |
| `STATUS` | Worker → Coordinator | Progress update |
| `RESULT` | Worker → Coordinator | Final output |
| `CONTROL` | Either | Cancel, shutdown, heartbeat |
| `PERMISSION` | Worker → Coordinator | Worker needs approval |
| `APPROVAL` | Coordinator → Worker | Grant or deny permission |

---

## 8. Common mistakes

### Mistake 1 — Using `shared_dict` instead of mailbox

```python
# WRONG — shared mutable state creates race conditions in async code
_worker_results = {}
async def worker_fn(task):
    _worker_results[task.task_id] = "done"
```

**Fix:** use `await mailbox.send(AgentMessage.result(...))`.

### Mistake 2 — Sending plain text instead of typed messages

```python
# WRONG — coordinator cannot distinguish status from result
await mailbox.send(AgentMessage(sender=wid, recipient="coord", kind=MessageKind.STATUS, payload={"text": "done with research and here is the final answer: ..."}))
```

**Fix:** separate status (progress) from result (final output). Use `MessageKind.RESULT` for final answers.

### Mistake 3 — Not checking for cancellation in the worker loop

```python
# WRONG — worker runs forever even if cancelled
async for event in worker_agent.run(task.prompt):
    pass  # never checks mailbox
```

**Fix:** inside the event loop, drain and process mailbox messages each iteration.

---

## 9. Exercises

**Exercise A — Worker heartbeat**

Every 5 tool calls, the worker automatically sends a `STATUS` message to the coordinator with the current turn count. The coordinator can use `check_my_mailbox` to see progress on long-running tasks.

**Exercise B — Permission routing through mailbox**

Update the worker's confirmation callback to send a `PERMISSION` message to the coordinator instead of prompting stdin directly. The coordinator receives it via `check_my_mailbox`, shows it to the user, then sends an `APPROVAL` response back to the worker.

**Exercise C — Message log**

Create `InMemoryMailbox.message_log: list[AgentMessage]` that records every message ever sent (not just pending ones). Add a `/msgs` REPL command that prints the last 20 log entries.

---

## 10. Checklist before moving on

- [ ] `AgentMessage` has sender, recipient, kind, payload, task_id, correlation_id, timestamp
- [ ] `MessageKind` has COMMAND, STATUS, RESULT, CONTROL, PERMISSION, APPROVAL
- [ ] `InMemoryMailbox` drains all pending messages non-blocking in `receive_all()`
- [ ] Workers check their mailbox each loop iteration for CANCEL and COMMAND messages
- [ ] Workers send STATUS messages after each tool call
- [ ] Workers send RESULT message on completion
- [ ] `SendWorkerMessageTool` sends COMMAND or CONTROL messages to a worker
- [ ] `CheckWorkerMailboxTool` drains coordinator's pending messages
- [ ] `correlation_id` links a PERMISSION request to its APPROVAL response
- [ ] No shared mutable dicts between coordinator and workers
- [ ] `FileMailbox` is available as a durable alternative to `InMemoryMailbox`

### Improvement: durable `FileMailbox`

`InMemoryMailbox` loses all messages if the coordinator process restarts while a worker is mid-task. The fix is straightforward — write each message to a JSON file:

```python
# agent/mailbox.py  — add FileMailbox alongside InMemoryMailbox

import json
from pathlib import Path

class FileMailbox(Mailbox):
    """
    Durable mailbox backed by the filesystem.

    Each message is one JSON file: mailbox/{recipient}/{message_id}.json
    Messages are consumed (deleted) when received.
    Survives coordinator restarts.
    """

    def __init__(self, root: Path = Path("mailbox")) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, recipient: str) -> Path:
        d = self.root / recipient
        d.mkdir(exist_ok=True)
        return d

    async def send(self, message: AgentMessage) -> None:
        path = self._dir(message.recipient) / f"{message.message_id}.json"
        path.write_text(
            json.dumps(message.__dict__ if hasattr(message, "__dict__") else {
                "sender": message.sender, "recipient": message.recipient,
                "kind": message.kind.value, "payload": message.payload,
                "task_id": message.task_id, "correlation_id": message.correlation_id,
                "timestamp": message.timestamp, "message_id": message.message_id,
            }, indent=2),
            encoding="utf-8",
        )

    async def receive_all(self, recipient: str) -> list[AgentMessage]:
        d = self._dir(recipient)
        messages = []
        for path in sorted(d.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                messages.append(AgentMessage(
                    sender=data["sender"], recipient=data["recipient"],
                    kind=MessageKind(data["kind"]), payload=data.get("payload", {}),
                    task_id=data.get("task_id"), correlation_id=data.get("correlation_id"),
                    timestamp=data.get("timestamp", ""), message_id=data.get("message_id", ""),
                ))
                path.unlink()   # consume (delete) the message file
            except Exception:
                pass   # skip corrupt files
        return messages
```

Use `FileMailbox` in production, `InMemoryMailbox` in tests. Swap in `build_agent()`:

```python
mailbox = FileMailbox(root=Path("mailbox"))   # durable
# mailbox = InMemoryMailbox()                  # in-memory (tests)
```

---

Next: [12-dangerous-actions-and-user-confirmation.md](12-dangerous-actions-and-user-confirmation.md)

