from __future__ import annotations

import logging
from importlib import util
from pathlib import Path
from typing import Callable

from nexus.hooks import HookExecutor
from nexus.tools.base import BaseTool, ToolRegistry


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
            spec = util.spec_from_file_location(path.stem, path)
            if spec is None or spec.loader is None:
                logger.warning("Skipping plugin with invalid import spec: %s", path.name)
                continue
            module = util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                logger.warning("Plugin load failed for %s: %s", path.stem, exc)
                continue
            register = getattr(module, "register", None)
            if register is None:
                logger.warning("Plugin %s has no register() function.", path.stem)
                continue
            try:
                register(_PluginRegistryView(registry, path.stem, can_register=can_register), hooks)
            except Exception as exc:
                logger.warning("Plugin register() failed for %s: %s", path.stem, exc)
                continue
            loaded.append(path.stem)
        return loaded