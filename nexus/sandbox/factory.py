"""Registry integration factories for sandbox and agent special tools.

These functions are the only entry-points that :mod:`nexus.app` needs.  They
keep all sandbox/sub-agent wiring out of the application bootstrap so the
package remains self-contained.
"""
from __future__ import annotations

import logging

from nexus.sandbox.docker import DockerSandbox, SandboxConfig, docker_available, docker_image_available
from nexus.sandbox.tool import SandboxedCommandTool
from nexus.tools.base import ToolRegistry

logger = logging.getLogger(__name__)


def register_sandbox_tool(registry: ToolRegistry, config) -> bool:
    """Conditionally register :class:`~nexus.sandbox.tool.SandboxedCommandTool`.

    Performs Docker availability checks and emits warnings when Docker or the
    configured sandbox image is missing.  The tool is registered only when
    all preconditions are satisfied.

    Parameters
    ----------
    registry:
        The :class:`~nexus.tools.base.ToolRegistry` to register into.
    config:
        An :class:`~nexus.config.defaults.AgentConfig` instance that provides
        ``sandbox_commands``, ``sandbox_image``, ``sandbox_timeout_seconds``,
        ``sandbox_memory_limit``, ``sandbox_network``,
        ``sandbox_read_only_workspace``, and ``sandbox_tmp_size``.

    Returns
    -------
    bool
        ``True`` if the tool was registered; ``False`` if it was skipped.
    """
    if not config.sandbox_commands:
        return False

    image = config.sandbox_image
    if not docker_available():
        logger.warning("Sandboxed command tool requested but Docker is not available.")
        return False

    if not docker_image_available(image):
        logger.warning(
            "Sandbox image %s is not built. Run: docker build -f nexus/Dockerfile.sandbox -t %s .",
            image,
            image,
        )
        return False

    sandbox = DockerSandbox(
        SandboxConfig(
            image=image,
            timeout_seconds=float(config.sandbox_timeout_seconds),
            memory_limit=config.sandbox_memory_limit,
            network=config.sandbox_network,
            read_only_workspace=config.sandbox_read_only_workspace,
            tmp_size=config.sandbox_tmp_size,
        )
    )
    registry.register(SandboxedCommandTool(sandbox), source="sandbox", origin=image)
    return True


def register_agent_tool(
    registry: ToolRegistry,
    delegation,
    config,
) -> bool:
    """Compatibility wrapper for cognitive sub-agent registration.

    The ``delegation`` argument is ignored for old callers; cognitive
    sub-agents are normal tools and do not require a worker runtime.
    """
    del delegation

    from nexus.tools.subagents import register_subagent_tools

    return register_subagent_tools(registry, config) > 0
