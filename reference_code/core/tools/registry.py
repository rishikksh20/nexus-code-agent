import logging
from pathlib import Path
from typing import Any

from core.config.config import Config
from core.hooks.hook_system import HookSystem
from core.safety.approval import ApprovalContext, ApprovalDecision, ApprovalManager
from core.tools.base import Tool, ToolResult, ToolInvocation
from core.tools.base import Tool
from core.tools.builtin.read_file import ReadFileTool
from core.tools.builtin import get_all_builtin_tools
from core.tools.subagents import get_default_subagent_definitions, SubagentTool

logger = logging.getLogger(__name__)

class ToolRegistry:

    def __init__(self, config):
        self._tools: dict[str, Tool] = {}
        self._mcp_tools: dict[str, Tool] = {}
        self.config = config

    def get(self, name: str) -> Tool | None:
        if name in self._tools:
            return self._tools[name]
        elif name in self._mcp_tools:
            return self._mcp_tools[name]

        return None

    @property
    def connected_mcp_servers(self) -> list[Tool]:
        return self._mcp_tools.values()

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            logger.warning(f"Tool {tool.name} is already registered. Overwriting.")
        self._tools[tool.name] = tool
        logger.debug(f"Registered tool {tool.name}")
        return tool

    def register_mcp_tool(self, tool: Tool) -> None:
        self._mcp_tools[tool.name] = tool
        logger.debug(f"Registered MCP tool: {tool.name}")

    def unregister(self, tool: Tool) -> bool:
        if tool.name in self._tools:
            del self._tools[tool.name]
            logger.debug(f"Unregistered tool {tool.name}")
            return True
        logger.warning(f"Tool {tool.name} is not registered.")
        return False

    def get_tools(self) -> list[Tool]:
        tools: list[Tool] = []

        for tool in self._tools.values():
            tools.append(tool)

        for mcp_tool in self._mcp_tools.values():
            tools.append(mcp_tool)

        if self.config.allowed_tools:
            allowed_set = set(self.config.allowed_tools)
            tools = [t for t in tools if t.name in allowed_set]

        return tools

    def get_schemas(self) -> list[dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self.get_tools()]


    async def invoke(self, name: str, params: dict[str, Any], cwd: Path, hook_system: HookSystem, approval_manager: ApprovalManager|None) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            result = ToolResult.error_result(
                f"Unknown tool: {name}",
                metadata={"tool_name": name},
            )
            await hook_system.trigger_after_tool(name, params, result)
            return result
        
        validation_errors = tool.validate_params(params)

        if validation_errors:
            result = ToolResult.error_result(
                f"Invalid parameters: {'; '.join(validation_errors)}",
                metadata={
                    "tool_name": name,
                    "validation_errors": validation_errors,
                },
            )

            await hook_system.trigger_after_tool(name, params, result)

            return result
        await hook_system.trigger_before_tool(name, params)
        invocation = ToolInvocation(cwd=cwd, params=params)
        if approval_manager:
            confirmation = await tool.get_confirmation(invocation)
            if confirmation:
                context = ApprovalContext(
                    tool_name=tool.name,
                    params=params,
                    is_mutating=tool.is_mutating(params),
                    command=confirmation.command,
                    affected_paths=confirmation.affected_paths,
                    is_dangerous=confirmation.is_dangerous,
                )
                decision = await approval_manager.check_approval(context)
                if decision == ApprovalDecision.REJECTED:
                    result = ToolResult.error_result(
                        "Operation rejected by safety policy"
                    )
                    await hook_system.trigger_after_tool(name, params, result)
                    return result
                elif decision == ApprovalDecision.NEEDS_CONFIRMATION:
                    approved = approval_manager.request_confirmation(confirmation)

                    if not approved:
                        result = ToolResult.error_result("User rejected the operation")
                        await hook_system.trigger_after_tool(name, params, result)
                        return result
        try:
            result = await tool.execute(invocation)
        except Exception as e:
            logger.exception(f"Tool {name} raised unexpected error")
            result = ToolResult.error_result(
                f"Internal error: {str(e)}",
                metadata={
                    "tool_name",
                    name,
                },
            )

        await hook_system.trigger_after_tool(name, params, result)
        return result



def create_default_registry(config: Config) -> ToolRegistry:
    registry = ToolRegistry(config)

    BUITIN_TOOLS = get_all_builtin_tools()
    for tool_cls in BUITIN_TOOLS:
        registry.register(tool_cls(config))

    for subagent_def in get_default_subagent_definitions():
        registry.register(SubagentTool(config, subagent_def))
    return registry