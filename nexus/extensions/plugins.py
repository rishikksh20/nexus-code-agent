from __future__ import annotations

import logging
from importlib import util
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from nexus.hooks import HookExecutor
from nexus.tools.base import BaseTool, ToolRegistry

if TYPE_CHECKING:
    from nexus.config.defaults import AgentConfig


logger = logging.getLogger(__name__)


class _PluginRegistryView:
    def __init__(
        self,
        registry: ToolRegistry,
        plugin_name: str,
        *,
        can_register: Callable[[BaseTool], bool] | None = None,
    ) -> None:
        self._registry = registry
        self._plugin_name = plugin_name
        self._can_register = can_register or (lambda tool: True)

    def register(self, tool: BaseTool) -> None:
        if not self._can_register(tool):
            logger.info("Skipping plugin tool %s from %s due to policy.", tool.name, self._plugin_name)
            return
        self._registry.register(tool, source="plugin", origin=self._plugin_name)


class PluginLoader:
    def __init__(self, plugin_dir: Path) -> None:
        self.plugin_dir = plugin_dir

    def load_all(
        self,
        registry: ToolRegistry,
        hooks: HookExecutor,
        *,
        can_register: Callable[[BaseTool], bool] | None = None,
    ) -> list[str]:
        loaded: list[str] = []
        if not self.plugin_dir.exists():
            return loaded
        for path in sorted(self.plugin_dir.glob("*.py")):
            if self.load_path(path, registry, hooks, can_register=can_register):
                loaded.append(path.stem)
        return loaded

    def load_path(
        self,
        path: Path,
        registry: ToolRegistry,
        hooks: HookExecutor,
        *,
        can_register: Callable[[BaseTool], bool] | None = None,
    ) -> bool:
        spec = util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            logger.warning("Skipping plugin with invalid import spec: %s", path.name)
            return False
        module = util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            logger.warning("Plugin load failed for %s: %s", path.stem, exc)
            return False
        register = getattr(module, "register", None)
        if register is None:
            logger.warning("Plugin %s has no register() function.", path.stem)
            return False
        try:
            register(_PluginRegistryView(registry, path.stem, can_register=can_register), hooks)
        except Exception as exc:
            logger.warning("Plugin register() failed for %s: %s", path.stem, exc)
            return False
        return True


def get_plugin_roots(config: "AgentConfig") -> tuple[Path, ...]:
    """Return plugin roots in override order, with project tools last."""
    roots = [
        config.plugins_dir,
        config.local_root / "plugins",
        config.workspace_root / ".agents" / "tools",
    ]
    return tuple(_unique_paths(roots))


def load_plugins_from_roots(
    roots: tuple[Path, ...],
    registry: ToolRegistry,
    hooks: HookExecutor,
    *,
    can_register: Callable[[BaseTool], bool] | None = None,
) -> list[str]:
    """Load one plugin file per stem, preferring files from later roots."""
    selected: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*.py")):
            selected[path.stem] = path

    loaded: list[str] = []
    for name, path in sorted(selected.items()):
        if PluginLoader(path.parent).load_path(path, registry, hooks, can_register=can_register):
            loaded.append(name)
    return loaded


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique
