"""Backward-compatibility shim for nexus.runtime.sandbox.

The canonical implementation now lives in :mod:`nexus.sandbox`.
"""
from nexus.sandbox import (  # noqa: F401
    DockerSandbox,
    SandboxConfig,
    SandboxedCommandTool,
    docker_available,
    docker_image_available,
)

__all__ = [
    "DockerSandbox",
    "SandboxConfig",
    "SandboxedCommandTool",
    "docker_available",
    "docker_image_available",
]
