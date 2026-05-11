# Chapter 7: MCP, Plugins, And Sandboxing

## Objective

Open the harness to external capabilities without turning it into an unsafe or unmaintainable runtime. This chapter pulls together three important advanced improvements from `openai-code-tutorial`:

- MCP integration for standardized external tool servers
- plugin loading for local extension
- Docker-style sandboxing for hard execution boundaries

It also follows the broader lesson from `agentic-framework-tutorial`: extensibility is only useful when it preserves inspectability.

## Why Extension Must Come After Safety

If you add external tool loading before you have:

- typed tool calls
- permission enforcement
- logging
- execution modes

then your harness becomes a bag of callable side effects. That is not a platform. It is a liability.

## Add MCP As A First-Class Integration

MCP is valuable because it standardizes how tools can be exposed by external servers. Treat it as an adapter layer, not as a replacement for your internal tool abstraction.

```python
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class MCPToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class MCPClient:
    async def list_tools(self) -> list[MCPToolSpec]:
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        ...
```

Now adapt those specs into your internal registry.

```python
class MCPToolAdapter:
    def __init__(self, client: MCPClient, spec: MCPToolSpec) -> None:
        self.client = client
        self.name = spec.name
        self.description = spec.description
        self.is_mutating = True

    async def execute(self, call_id: str, arguments: dict, context: ToolExecutionContext) -> ToolResult:
        # call_id comes from the ToolCall object; do not hard-code it
        output = await self.client.call_tool(self.name, arguments)
        return ToolResult(call_id=call_id, tool_name=self.name, output=output)
```

The important design point is this: your agent runtime still sees a `BaseTool` shape. MCP stays behind an adapter boundary.

## Add A Plugin Loader

Plugins are different from MCP. They live inside your Python environment and can register local tools, hooks, or startup logic.

```python
from importlib import util
from pathlib import Path


class PluginLoader:
    def __init__(self, plugin_dir: Path) -> None:
        self.plugin_dir = plugin_dir

    def load_all(self, registry: ToolRegistry, hooks: HookExecutor) -> None:
        for path in self.plugin_dir.glob("*.py"):
            spec = util.spec_from_file_location(path.stem, path)
            if spec is None or spec.loader is None:
                continue
            module = util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                # Log and skip broken plugins; do not crash the whole startup
                import logging
                logging.getLogger(__name__).warning(
                    "Plugin load failed", extra={"plugin": path.stem, "error": str(exc)}
                )
                continue
            if hasattr(module, "register"):
                module.register(registry, hooks)
```

### Good Plugin Rules

- plugins should register capabilities, not patch core runtime behavior silently
- plugin loading should be logged
- plugin failures should not corrupt the whole startup path
- plugin APIs should stay narrow and explicit

## Add Execution Sandboxing

This is one of the most valuable safety upgrades in the OpenAI-oriented tutorial. Path checks and prompt instructions are not enough if a tool can spawn arbitrary execution.

Create a sandbox boundary for dangerous tools such as shell execution, arbitrary Python, or network access.

```python
import asyncio


class SandboxedCommandTool:
    name = "run_command"
    description = "Execute a command in an isolated container."
    is_mutating = True

    async def execute(self, call_id: str, arguments: dict, context: ToolExecutionContext) -> ToolResult:
        command = arguments["command"]
        timeout_seconds = 30  # always set a timeout; hung containers block the event loop
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "run",
                "--rm",
                "python:3.11-slim",
                "sh",
                "-lc",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Command timed out.", is_error=True)
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=stdout.decode(),
            is_error=process.returncode != 0,
        )
```

This example is intentionally simple. In a real harness you would also control:

- mounted paths
- network availability
- CPU and memory limits
- timeout limits
- image allowlists

## Extension Ordering

Add extensions in this order:

1. internal tools
2. plugin-based local extensions
3. MCP external tools
4. sandboxed execution tools

This order keeps failures understandable.

## Action Plan

1. Create an MCP adapter layer that converts external tool specs into internal tools.
2. Add a local plugin loader with a minimal `register()` contract.
3. Log all plugin and MCP registrations at startup.
4. Restrict dangerous tools to a sandbox boundary.
5. Keep permission policy and execution isolation separate.
6. Mark externally sourced tools clearly in logs and UI.

## Validation Checklist

- External MCP tools can be listed and executed through the internal registry.
- Plugin loading failures do not crash the entire harness.
- Sandboxed tools run behind a technical boundary, not just a prompt rule.
- Dangerous tools still consult the permission system.
- Logs identify whether a tool came from core, plugin, or MCP.

## Definition Of Done

This chapter is complete when your harness can grow safely. If external capabilities still look indistinguishable from built-in ones, you have not finished the observability part of the design.