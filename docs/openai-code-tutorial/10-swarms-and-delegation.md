# 10 — Swarms and Delegation: Coordinator + Worker Agents

## Prerequisites

Complete [09-plan-mode-and-auto-mode.md](09-plan-mode-and-auto-mode.md) first.

Your single agent is now capable, permission-aware, and mode-controlled. The next step: delegate subproblems to **worker agents** that run focused, narrower tasks while the coordinator stays in charge of the overall workflow.

---

## What you will build

```
agent/
    swarm.py        ← NEW: SpawnRequest, TaskRecord, TaskStatus, SwarmBackend
    tools.py        ← updated: SpawnWorkerTool, GetWorkerResultTool
main.py             ← updated: coordinator mode display
```

---

## 1. When delegation helps (and when it doesn't)

**Delegate when:**
- The subproblem has a clear boundary ("research only", "test only")
- Work can run in parallel with other tasks
- A specialized role improves quality (researcher, tester, reviewer)
- The coordinator would lose focus doing it inline

**Do NOT delegate when:**
- The task is tiny (overhead exceeds the work)
- The scope is unclear (ambiguous tasks produce ambiguous results)
- The coordinator still needs more context first
- You are about to delegate delegation (multi-level nesting without clear boundaries)

The coordinator should always ask: *will this delegation make the system clearer or faster?*

---

## 2. The coordinator-worker mental model

```
┌────────────────────────────────────────────────────────┐
│                    COORDINATOR                         │
│  - decides what to delegate                            │
│  - issues SpawnRequest                                 │
│  - tracks TaskRecord (lifecycle state)                 │
│  - collects worker result                              │
│  - decides what happens next                           │
└──────────────────┬─────────────────────────────────────┘
                   │ SpawnRequest
                   ▼
┌────────────────────────────────────────────────────────┐
│                      WORKER                            │
│  - receives a focused task + narrow tools              │
│  - runs its own mini agent loop                        │
│  - writes result to TaskRecord                         │
│  - terminates (does not live forever)                  │
└────────────────────────────────────────────────────────┘
```

Workers are **tasks with a lifecycle**, not ghost processes running in the background.

---

## 3. Create `agent/swarm.py`

```python
# agent/swarm.py

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ── Task lifecycle ────────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    REQUESTED  = "requested"
    RUNNING    = "running"
    WAITING    = "waiting"     # worker paused for coordinator input
    COMPLETED  = "completed"
    FAILED     = "failed"
    KILLED     = "killed"

    @property
    def is_terminal(self) -> bool:
        return self in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.KILLED}


# ── Spawn request ─────────────────────────────────────────────────────────────

@dataclass
class SpawnRequest:
    """
    Structured request to create a worker agent for a focused subproblem.

    description  — one-line label for the task
    prompt       — the actual instruction given to the worker
    role         — specialization hint (researcher, tester, reviewer, implementer)
    allowed_tools — if set, restricts the worker to only these tools
    mode         — the execution mode the worker should run in
    constraints  — any extra policy or scope boundaries
    """
    description: str
    prompt: str
    role: str = "assistant"
    allowed_tools: list[str] = field(default_factory=list)
    mode: str = "default"
    constraints: dict[str, Any] = field(default_factory=dict)


# ── Task record ───────────────────────────────────────────────────────────────

@dataclass
class TaskRecord:
    """
    Durable record for one delegated task.

    This is what you inspect when you want to know what a worker did.
    All fields are serializable (no live objects).
    """
    task_id: str
    worker_id: str
    role: str
    description: str
    prompt: str
    status: TaskStatus
    allowed_tools: list[str]
    mode: str
    result: str = ""           # final output from the worker
    error: str = ""            # set if status is FAILED
    created_at: str = ""
    completed_at: str = ""
    notes: list[str] = field(default_factory=list)  # progress notes from the worker

    @classmethod
    def from_request(cls, request: SpawnRequest) -> "TaskRecord":
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return cls(
            task_id=uuid.uuid4().hex[:8],
            worker_id=uuid.uuid4().hex[:8],
            role=request.role,
            description=request.description,
            prompt=request.prompt,
            status=TaskStatus.REQUESTED,
            allowed_tools=request.allowed_tools,
            mode=request.mode,
            created_at=now,
        )

    def complete(self, result: str) -> None:
        self.status = TaskStatus.COMPLETED
        self.result = result
        self.completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def fail(self, error: str) -> None:
        self.status = TaskStatus.FAILED
        self.error = error
        self.completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "role": self.role,
            "description": self.description,
            "prompt": self.prompt,
            "status": self.status.value,
            "allowed_tools": self.allowed_tools,
            "mode": self.mode,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "notes": self.notes,
        }


# ── Swarm backend ─────────────────────────────────────────────────────────────

class SwarmBackend:
    """
    Manages the lifecycle of delegated worker tasks.

    The coordinator interacts with this backend — it never manages
    asyncio tasks or processes directly.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}

    async def spawn(
        self,
        request: SpawnRequest,
        worker_fn,  # async callable(task: TaskRecord) -> None
    ) -> TaskRecord:
        """
        Create a task record and start the worker coroutine.

        The worker_fn is responsible for:
        1. Setting task.status = TaskStatus.RUNNING
        2. Doing the actual work
        3. Calling task.complete(result) or task.fail(error)
        """
        task = TaskRecord.from_request(request)
        self._tasks[task.task_id] = task
        # Launch the worker without blocking the coordinator
        asyncio.create_task(worker_fn(task))
        return task

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[TaskRecord]:
        return list(self._tasks.values())

    def list_active(self) -> list[TaskRecord]:
        return [t for t in self._tasks.values() if not t.status.is_terminal]

    async def wait_for(self, task_id: str, timeout: float = 60.0) -> TaskRecord | None:
        """Poll until the task reaches a terminal state or timeout expires."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        deadline = asyncio.get_event_loop().time() + timeout
        while not task.status.is_terminal:
            if asyncio.get_event_loop().time() > deadline:
                task.fail(f"Task timed out after {timeout}s")
                break
            await asyncio.sleep(0.5)
        return task


# ── Local worker runner ───────────────────────────────────────────────────────

def make_local_worker_fn(model_client, tool_registry_factory, base_prompt: str, cwd: str):
    """
    Factory: returns an async worker_fn that runs a mini agent loop for a task.

    tool_registry_factory is called with allowed_tools to build a restricted registry.
    """
    from agent.agent import Agent
    from agent.modes import ExecutionMode

    async def worker_fn(task: TaskRecord) -> None:
        task.status = TaskStatus.RUNNING
        task.notes.append(f"Worker started: role={task.role}")

        # Build a restricted registry for this worker
        registry = tool_registry_factory(task.allowed_tools)

        worker_agent = Agent(
            model_client=model_client,
            tool_registry=registry,
            base_prompt=(
                f"{base_prompt}\n\n"
                f"You are a specialized worker agent.\n"
                f"Role: {task.role}\n"
                f"Your task: {task.description}\n"
                f"Complete the task and stop. Do not ask follow-up questions."
            ),
            cwd=cwd,
            model_name="worker",
            mode=ExecutionMode(task.mode),
        )

        output_parts = []
        try:
            from agent.events import AssistantTextDelta, ToolExecutionCompleted
            async for event in worker_agent.run(task.prompt):
                if isinstance(event, AssistantTextDelta):
                    output_parts.append(event.text)
                elif isinstance(event, ToolExecutionCompleted) and not event.is_error:
                    task.notes.append(f"  tool: {event.tool_name} → {event.output[:80]}")

            task.complete("\n".join(output_parts) or "(no text output)")
        except Exception as exc:
            task.fail(str(exc))

    return worker_fn
```

---

## 4. Add delegation tools to `agent/tools.py`

```python
# agent/tools.py  — add SpawnWorkerTool and GetWorkerResultTool

from agent.swarm import SpawnRequest, SwarmBackend, TaskStatus


class SpawnWorkerTool(BaseTool):
    """
    Delegate a focused subproblem to a worker agent.

    The worker runs asynchronously. Use GetWorkerResultTool to check when it is done.
    """
    name = "spawn_worker"
    description = (
        "Create a worker agent to handle a focused subtask. "
        "Give the worker a narrow, specific prompt with a clear stop condition. "
        "Returns a task_id you can use to check progress."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "One-line label for this task."},
            "prompt":      {"type": "string", "description": "Full instruction for the worker."},
            "role":        {"type": "string", "description": "Worker role: researcher, tester, reviewer, implementer."},
            "tools":       {"type": "array", "items": {"type": "string"}, "description": "Tools the worker may use."},
            "mode":        {"type": "string", "enum": ["default", "plan", "auto"], "description": "Worker execution mode."},
        },
        "required": ["description", "prompt"],
    }
    is_mutating = True  # spawning workers has side effects

    def __init__(self, backend: SwarmBackend, worker_fn_factory) -> None:
        self._backend = backend
        self._factory = worker_fn_factory

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        request = SpawnRequest(
            description=arguments["description"],
            prompt=arguments["prompt"],
            role=arguments.get("role", "assistant"),
            allowed_tools=arguments.get("tools", []),
            mode=arguments.get("mode", "default"),
        )
        worker_fn = self._factory()
        task = await self._backend.spawn(request, worker_fn)
        return ToolResult(
            output=f"Worker spawned. task_id={task.task_id}, role={task.role}",
            metadata={"task_id": task.task_id, "worker_id": task.worker_id},
        )


class GetWorkerResultTool(BaseTool):
    """
    Check the status and result of a delegated worker task.
    """
    name = "get_worker_result"
    description = (
        "Check the status and result of a worker task. "
        "Pass the task_id returned by spawn_worker."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task_id to check."},
            "wait":    {"type": "boolean", "description": "If true, wait up to 60s for completion."},
        },
        "required": ["task_id"],
    }
    is_mutating = False

    def __init__(self, backend: SwarmBackend) -> None:
        self._backend = backend

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        task_id = arguments.get("task_id", "").strip()
        wait = arguments.get("wait", False)

        if not task_id:
            return ToolResult(output="Error: 'task_id' is required.", is_error=True)

        if wait:
            task = await self._backend.wait_for(task_id, timeout=60.0)
        else:
            task = self._backend.get(task_id)

        if task is None:
            return ToolResult(output=f"Task '{task_id}' not found.", is_error=True)

        lines = [
            f"Task: {task.task_id}",
            f"Status: {task.status.value}",
            f"Role: {task.role}",
            f"Description: {task.description}",
        ]
        if task.notes:
            lines.append("Progress:")
            lines.extend(f"  {n}" for n in task.notes[-5:])  # last 5 notes
        if task.status == TaskStatus.COMPLETED:
            lines.append(f"Result:\n{task.result}")
        elif task.status == TaskStatus.FAILED:
            lines.append(f"Error: {task.error}")

        return ToolResult(
            output="\n".join(lines),
            metadata=task.to_dict(),
        )
```

---

## 5. Update `main.py` — add swarm display and REPL command

```python
# main.py  — add tasks command and swarm setup

from agent.swarm import SwarmBackend, make_local_worker_fn

def build_agent(project_notes: str = "", mode: ExecutionMode = ExecutionMode.DEFAULT) -> Agent:
    client = DemoModelClient()
    memory_store = MemoryStore(root=Path(".agent-memory"))
    skill_registry = load_skills_from_dir(Path("skills"))

    # ── Swarm backend ─────────────────────────────────────────────────────────
    backend = SwarmBackend()

    def tool_registry_factory(allowed_tools: list[str]):
        """Build a restricted registry for workers."""
        full_registry = default_registry(memory_store=memory_store)
        if not allowed_tools:
            return full_registry  # no restriction
        restricted = ToolRegistry()
        for name in allowed_tools:
            tool = full_registry.get(name)
            if tool:
                restricted.register(tool)
        return restricted

    def worker_fn_factory():
        return make_local_worker_fn(
            model_client=client,
            tool_registry_factory=tool_registry_factory,
            base_prompt=DEFAULT_BASE_PROMPT,
            cwd=__import__("os").getcwd(),
        )

    registry = default_registry(memory_store=memory_store)
    if skill_registry.names():
        registry.register(SkillTool(skill_registry))
    registry.register(SpawnWorkerTool(backend, worker_fn_factory))
    registry.register(GetWorkerResultTool(backend))

    # ...rest of build_agent()...
    return Agent(...)


# In repl() — add /tasks command
if user_input == "/tasks":
    tasks = backend.list_tasks()
    if not tasks:
        print("No tasks.\n")
    else:
        print(f"\n── Worker tasks ({len(tasks)}) ──")
        for t in tasks:
            print(f"  {t.task_id}  {t.status.value:10}  [{t.role}]  {t.description[:50]}")
        print()
    continue
```

---

## 6. Writing good worker prompts

```
BAD:   "Help with the auth module."
GOOD:  "Read src/auth.py and src/tests/test_auth.py. 
        List all test cases that are missing coverage for the JWT refresh flow.
        Do not edit any files. Return a numbered list of missing tests.
        Stop after producing the list."
```

A good worker prompt has:
1. **Narrow scope** — exact files or directories
2. **Clear deliverable** — what to produce
3. **Role** — researcher, tester, reviewer, implementer
4. **Tool constraint** — which tools may be used
5. **Stop condition** — when to stop

---

## 7. Common mistakes

### Mistake 1 — Worker with no tool constraints

```python
# WRONG — researcher gets write tools and might mutate files
request = SpawnRequest(description="Research auth", prompt="...", role="researcher")
```

**Fix:** always set `allowed_tools` for workers. A researcher only needs `["read_file", "glob", "search_memory"]`.

### Mistake 2 — Checking result before worker finishes

```python
# WRONG — task is still RUNNING
tool_call "get_worker_result" task_id=abc123 wait=false
→ "Status: running, Result: (empty)"
```

**Fix:** use `wait=true` to block until done, or poll and check `status` before using `result`.

### Mistake 3 — Spawning workers for tiny tasks

```python
# WRONG — 3 lines of work, massive delegation overhead
spawn_worker("Get the current time", "Call get_time and return it.")
```

**Fix:** delegate only subproblems where setup overhead is justified by the work.

---

## 8. Exercises

**Exercise A — Worker results in session**

After a worker completes, save its `result` to `carry_over["last_worker_result"]` in the session snapshot. This lets the coordinator reference worker output in future turns.

**Exercise B — Parallel workers**

Extend the coordinator with a tool `run_parallel_workers` that spawns multiple workers from a list of prompts, waits for all with `asyncio.gather`, and returns all results.

**Exercise C — Task persistence**

Add JSON serialization to `TaskRecord.to_dict()` and save all completed tasks to `tasks/task-{id}.json` after completion. This gives you a durable audit trail of all delegated work.

---

## 9. Checklist before moving on

- [ ] `SpawnRequest` carries description, prompt, role, allowed_tools, mode
- [ ] `TaskRecord` has lifecycle states: requested, running, waiting, completed, failed, killed
- [ ] `TaskStatus.is_terminal` correctly identifies terminal states
- [ ] `SwarmBackend.spawn()` creates a `TaskRecord` and starts the worker as an async task
- [ ] `SpawnWorkerTool` is a mutating tool requiring confirmation/approval
- [ ] `GetWorkerResultTool` can wait for completion with `wait=true`
- [ ] Worker gets a restricted `ToolRegistry` based on `allowed_tools`
- [ ] Worker runs its own `Agent` loop with a focused prompt and stop condition
- [ ] `/tasks` REPL command lists all tasks with their status
- [ ] Coordinator never loses track of a worker — every spawn produces a `TaskRecord`
- [ ] Parallel worker results are correlated to the right coordinator step via `task_id`

### Improvement: worker result routing for parallel tasks

When multiple workers run concurrently, the coordinator receives results from all of them. Without correlation, it cannot tell which result belongs to which step.

**Pattern:** include a `correlation_key` in the spawn request payload and store it in `carry_over` alongside the `task_id`:

```python
# Coordinator spawns two parallel workers:
results_pending = {}

for step in ["research", "test"]:
    # spawn_worker returns task_id
    task_id = ...   # from SpawnWorkerTool result metadata
    results_pending[task_id] = step   # map task_id → step name

# coordinator stores in carry_over:
snapshot.carry_over["pending_tasks"] = results_pending

# Later, collect_worker_results:
for task_id, step_name in results_pending.items():
    task = backend.get(task_id)
    if task and task.status == TaskStatus.COMPLETED:
        # Route result to the right step
        snapshot.carry_over[f"result_{step_name}"] = task.result
```

The `task_id` returned by `SpawnWorkerTool` in its `metadata` field is the correlation handle — always store it.

---

Next: [11-agent-communication.md](11-agent-communication.md)

