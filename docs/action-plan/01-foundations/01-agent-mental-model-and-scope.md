# Chapter 1: Agent Mental Model And Scope

> **New to `async`/`await`?** Read [00-sync-to-async-primer.md](./00-sync-to-async-primer.md) first. It builds the same REPL three times — sync, async, then streaming — and explains every keyword before this chapter uses them.

## Objective

Build the smallest useful understanding of an agent harness before you write abstractions. In this chapter, you define what the harness is responsible for, what it should never do implicitly, and what the first minimal runtime should look like.

This chapter combines two key lessons from the tutorial sources:

- from `agentic-framework-tutorial`: think of the system as a control loop with explicit boundaries
- from `openai-code-tutorial`: start with the simplest working implementation and grow it in small, testable steps

## What You Are Building In This Chapter

You are not building the final harness yet. You are building:

- the vocabulary for the project
- a minimal REPL shell
- a fake model client so you can develop the runtime without network dependencies
- a clear statement of what belongs in the agent loop and what belongs outside it

## Core Mental Model

An agent harness is a program that repeatedly does the following:

1. receives a goal or follow-up input
2. builds context for the current turn
3. asks a model what should happen next
4. executes the requested action if allowed
5. feeds the result back into the loop
6. stops only when the task is done or the user interrupts

That means your harness is not just a chat UI. It is a runtime that coordinates model output, tool execution, permissions, state, and user interaction.

## Scope Boundaries

Before writing code, decide these boundaries.

### The Harness Must Own

- turn orchestration
- message history
- tool dispatch
- permission checks
- session persistence
- structured logging
- execution mode changes

### The Harness Must Not Hide

- whether a tool is mutating state
- whether a step needs user approval
- whether context was compacted or pruned
- whether a worker or plugin handled a step
- whether a message came from the model or a tool

This explicitness is a repeated theme across both tutorial sets. It keeps the system debuggable.

## Project Setup

Create the project with a small, boring layout first.

```text
agent_harness/
  __init__.py
  app.py
  fake_model.py
  tests/
```

Use a modern Python baseline:

- Python 3.11 or newer
- `asyncio`
- `dataclasses`
- `typing`
- `pathlib`
- `pytest`

## First REPL Skeleton

Start with a shell that proves the outer loop exists independently from the inner agent loop.

```python
import asyncio


async def repl() -> None:
    print("Minimal Agent Harness")
    print("Type 'quit' to exit.\n")

    while True:
        user_text = input("> ").strip()
        if user_text.lower() in {"quit", "exit"}:
            print("bye")
            return

        print(f"[repl] received: {user_text}")


if __name__ == "__main__":
    asyncio.run(repl())
```

This program is intentionally not intelligent yet. Its job is to show that the user-facing session loop exists before any model integration happens.

> **Note on `input()` in async code.** `input()` is a blocking call. In a production server with many concurrent users you would use `asyncio.StreamReader` or a proper async readline. For a single-user CLI agent this is fine: the loop has exactly one user, so blocking on input does not starve anything else. When you see `asyncio.run(repl())`, you are simply asking asyncio to own the event loop so every awaitable you add later (model calls, tool I/O, file writes) can use `async`/`await` without introducing threads.

## Add A Fake Model Early

Do not start with a real API call. A fake model gives you deterministic development and makes later tests easier.

```python
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class FakeResponse:
    text: str
    stop_reason: str = "done"


class FakeModelClient:
    async def complete(self, prompt: str) -> FakeResponse:
        if "time" in prompt.lower():
            return FakeResponse(text="I should call a time tool for that.")
        return FakeResponse(text=f"Echo: {prompt}")
```

Now wire it into the REPL.

```python
import asyncio

from fake_model import FakeModelClient


async def repl() -> None:
    model = FakeModelClient()

    while True:
        user_text = input("> ").strip()
        if user_text.lower() in {"quit", "exit"}:
            return

        response = await model.complete(user_text)
        print(response.text)


if __name__ == "__main__":
    asyncio.run(repl())
```

## Why This Small Start Matters

This chapter protects you from a common failure mode: developers jump into tools, memory, and streaming before the system has a clean control surface. That leads to a harness where the model, the UI, and side effects are mixed together.

The minimal structure above keeps three future layers visible:

- the outer REPL loop
- the model interaction layer
- the tool execution layer you will add next

## Chapter Action Plan

1. Create a tiny Python package for the harness.
2. Add a REPL that only manages user input and output.
3. Add a fake model client with deterministic responses.
4. Verify that no tool or persistence logic is inside the REPL itself.
5. Write down your current scope boundaries in a project note.

## Validation Checklist

- The REPL can run without any network dependency.
- Exiting the program is explicit and clean.
- The model client can be swapped without changing the REPL loop.
- The code does not yet perform file writes or shell execution.

## Definition Of Done

You are ready for Chapter 2 when you can answer these questions clearly:

- What is the difference between the REPL loop and the agent loop?
- Why is a fake model useful even if you plan to use a real provider?
- Which responsibilities belong to the harness runtime and which do not?

If those answers are still fuzzy, do not continue yet. The rest of the architecture depends on them.