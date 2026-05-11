"""Sandbox and special-tool package for Nexus.

This package encapsulates all Docker-sandbox machinery and the higher-level
*special tools* that wrap external execution environments:

* :class:`~nexus.sandbox.docker.DockerSandbox` — container execution engine.
* :class:`~nexus.sandbox.tool.SandboxedCommandTool` — ``ToolKind.SANDBOX``
  tool that exposes the sandbox to the LLM.
* :class:`~nexus.sandbox.agent_tool.SubAgentTool` — ``ToolKind.AGENT``
  tool that delegates a sub-task to a worker via
  :class:`~nexus.runtime.delegation.DelegationRuntime`.

Public integration surface (what :mod:`nexus.app` imports):

* :func:`~nexus.sandbox.factory.register_sandbox_tool` — checks Docker
  availability then registers :class:`~nexus.sandbox.tool.SandboxedCommandTool`.
* :func:`~nexus.sandbox.factory.register_agent_tool` — registers
  :class:`~nexus.sandbox.agent_tool.SubAgentTool` when delegation is live.
"""

from nexus.sandbox.agent_tool import SubAgentTool
from nexus.sandbox.docker import DockerSandbox, SandboxConfig, docker_available, docker_image_available
from nexus.sandbox.factory import register_agent_tool, register_sandbox_tool
from nexus.sandbox.tool import SandboxedCommandTool

__all__ = [
    "DockerSandbox",
    "SandboxConfig",
    "SandboxedCommandTool",
    "SubAgentTool",
    "docker_available",
    "docker_image_available",
    "register_agent_tool",
    "register_sandbox_tool",
]
