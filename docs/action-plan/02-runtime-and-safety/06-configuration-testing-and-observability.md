# Chapter 6: Configuration, Testing, And Observability

## Objective

Make the harness maintainable. This chapter folds in three improvements that often get postponed too long:

- structured configuration
- dedicated test scaffolding
- structured logs and basic cost tracking

At this point the harness should already work. The purpose of this chapter is to make sure it can keep working as the feature set grows.

## Add Configuration First

Treat configuration as behavior control, not as a dumping ground for every constant.

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class AgentConfig:
    model_name: str
    session_dir: Path
    memory_dir: Path
    default_mode: str = "default"
    max_context_tokens: int = 12_000
    log_level: str = "INFO"
```

Load it from a TOML file and let environment variables override non-safety values.

Create `agent.toml` in your project root:

```toml
model_name = "gpt-4o-mini"
session_dir = ".agent/sessions"
memory_dir = ".agent/memory"
default_mode = "default"
max_context_tokens = 12000
log_level = "INFO"
```

Then load it:

```python
import os
import tomllib  # stdlib in Python 3.11+; install 'tomli' as a fallback for 3.10


def load_config(path: Path) -> AgentConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return AgentConfig(
        model_name=os.getenv("AGENT_MODEL", data["model_name"]),
        session_dir=Path(data["session_dir"]),
        memory_dir=Path(data["memory_dir"]),
        default_mode=os.getenv("AGENT_MODE", data.get("default_mode", "default")),
        max_context_tokens=int(os.getenv("AGENT_MAX_TOKENS", data.get("max_context_tokens", 12000))),
        log_level=os.getenv("AGENT_LOG_LEVEL", data.get("log_level", "INFO")),
    )
```

Do not put safety-critical path allowlists only in mutable config unless you have a very clear threat model. Defaults and hard boundaries should still be enforced in code.

## Build A Test Harness

One of the clearest strengths of `openai-code-tutorial` is that it explicitly teaches how to test the harness. Copy that discipline.

This chapter's `FakeModelClient` supersedes the simpler version from Chapter 1. The new version accepts a scripted list of `ModelResponse` objects so you can test multi-turn sequences deterministically.

```python
from models import Message, ModelResponse, ToolCall, ToolResult
from runtime.agent import Agent
from runtime.sessions import ToolExecutionContext
from tools.registry import ToolRegistry
from tools.builtin import GetTimeTool


class FakeModelClient:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses

    async def complete(self, messages: list[Message]) -> ModelResponse:
        return self._responses.pop(0)
```

Use that fake client to test the loop.

```python
import pytest

# These imports assume the project layout from Chapter 2's recommended package split.
# Adjust paths if your structure differs.
from models import Message, ModelResponse, ToolCall
from runtime.agent import Agent
from runtime.execution_modes import ExecutionMode
from runtime.sessions import ToolExecutionContext
from tools.builtin import GetTimeTool
from tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_agent_emits_tool_result(tmp_path):
    response = ModelResponse(
        message=Message(role="assistant", content="Checking time"),
        tool_calls=(ToolCall(call_id="1", tool_name="get_time", arguments={}),),
    )
    model = FakeModelClient([response])
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    agent = Agent(model, registry)
    context = ToolExecutionContext(session_id="s1", working_directory=tmp_path)

    events = [event async for event in agent.run([Message(role="user", content="what time is it")], context)]

    assert any(event["event"] == "tool_result" for event in events)
```

### What To Test First

- tool registration and lookup
- permission decisions
- session save and load
- context compaction behavior
- event emission sequence in the agent loop

## Add Structured Logging

Text logs are not enough once tools, plugins, and workers appear.

```python
import json
import logging
from datetime import datetime, UTC


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if hasattr(record, "session_id"):
            payload["session_id"] = record.session_id
        if hasattr(record, "tool_name"):
            payload["tool_name"] = record.tool_name
        return json.dumps(payload)
```

Use correlation data aggressively:

- session ID
- turn ID
- tool call ID
- worker ID when delegation is added

## Add Basic Usage And Cost Tracking

Even a simple harness should know roughly what it is spending.

```python
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class UsageSnapshot:
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
```

You do not need perfect cost accounting on day one. Approximate tracking is still better than flying blind.

## Action Plan

1. Add a typed `AgentConfig` and load it from TOML.
2. Allow environment-variable overrides for runtime behavior.
3. Build a fake model client and pytest coverage for the loop.
4. Add table-driven tests for permissions and sessions.
5. Log events in structured JSON.
6. Track basic token usage and estimated cost.

## Validation Checklist

- The harness can boot from configuration instead of hard-coded paths.
- Tests can run without a real model provider.
- Permission policies are unit-testable.
- Session persistence is covered by tests.
- Logs carry enough identifiers to reconstruct a failing turn.
- Cost data can be attached to a model response or turn record.

## Definition Of Done

This chapter is complete when you can confidently change one subsystem without breaking the others blindly. If you still rely on manual inspection alone, the harness is not ready for extension work.