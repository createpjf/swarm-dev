"""
core/plugin_sdk.py
Plugin/Hook extension system — runtime-loadable plugins that can inject
tools, hooks, and configuration into the Cleo agent stack.

Inspired by OpenClaw's plugin-sdk with 50+ hook points.

Plugin Structure:
  plugins/{name}/
    manifest.yaml     — name, version, hooks, tools, config schema
    __init__.py       — Python module with hook handlers
    tools.py          — Optional: custom tool definitions

Hook Points:
  - agent:bootstrap       — after agent init, before first task
  - session:created       — new channel session started
  - task:received         — task submitted to board
  - task:claimed          — agent claimed a task
  - task:completed        — task finished successfully
  - task:failed           — task failed with error
  - message:received      — incoming message from channel
  - message:sent          — outgoing message to channel
  - evolution:triggered   — evolution engine activated
  - memory:stored         — new memory stored (episode/case/pattern)

Usage:
    from core.plugin_sdk import PluginManager
    pm = PluginManager()
    pm.load_all()              # Scan plugins/ directory
    await pm.emit("task:completed", task_id="xxx", result="...")
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

PLUGIN_DIR = "plugins"

# All supported hook points
HOOK_POINTS = {
    "agent:bootstrap",
    "session:created",
    "task:received",
    "task:claimed",
    "task:completed",
    "task:failed",
    "message:received",
    "message:sent",
    "evolution:triggered",
    "memory:stored",
}


@dataclass
class PluginManifest:
    """Parsed plugin manifest."""
    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    hooks: list[str] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    config_schema: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass
class LoadedPlugin:
    """A loaded plugin with its module and manifest."""
    manifest: PluginManifest
    module: Any = None
    path: str = ""
    loaded_at: float = 0.0


class PluginManager:
    """Manages plugin lifecycle: discovery, loading, hook dispatch, tool registration."""

    def __init__(self, plugin_dir: str = PLUGIN_DIR):
        self.plugin_dir = plugin_dir
        self._plugins: dict[str, LoadedPlugin] = {}
        self._hooks: dict[str, list[Callable]] = {h: [] for h in HOOK_POINTS}
        self._tools: list[dict] = []
        os.makedirs(plugin_dir, exist_ok=True)

    def load_all(self) -> int:
        """Scan plugin directory and load all valid plugins. Returns count loaded."""
        count = 0
        if not os.path.isdir(self.plugin_dir):
            return 0

        for name in os.listdir(self.plugin_dir):
            plugin_path = os.path.join(self.plugin_dir, name)
            if not os.path.isdir(plugin_path):
                continue
            if name.startswith(".") or name.startswith("_"):
                continue
            try:
                self.load(name)
                count += 1
            except Exception as e:
                logger.warning("[plugin] failed to load '%s': %s", name, e)

        logger.info("[plugin] loaded %d plugins from %s", count, self.plugin_dir)
        return count

    def load(self, name: str) -> LoadedPlugin:
        """Load a single plugin by name."""
        plugin_path = os.path.join(self.plugin_dir, name)

        # Parse manifest
        manifest = self._parse_manifest(plugin_path, name)
        if not manifest.enabled:
            logger.info("[plugin] '%s' is disabled, skipping", name)
            raise ValueError(f"Plugin '{name}' is disabled")

        # Load Python module
        module = None
        init_path = os.path.join(plugin_path, "__init__.py")
        if os.path.exists(init_path):
            try:
                spec = importlib.util.spec_from_file_location(
                    f"plugins.{name}", init_path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[f"plugins.{name}"] = module
                    spec.loader.exec_module(module)
            except Exception as e:
                logger.warning("[plugin] '%s' module load failed: %s", name, e)

        plugin = LoadedPlugin(
            manifest=manifest,
            module=module,
            path=plugin_path,
            loaded_at=time.time(),
        )
        self._plugins[name] = plugin

        # Register hooks
        for hook_name in manifest.hooks:
            if hook_name not in HOOK_POINTS:
                logger.warning("[plugin] '%s' registers unknown hook '%s'",
                               name, hook_name)
                continue
            handler_name = f"on_{hook_name.replace(':', '_')}"
            if module and hasattr(module, handler_name):
                handler = getattr(module, handler_name)
                self._hooks[hook_name].append(handler)
                logger.debug("[plugin] '%s' registered hook %s", name, hook_name)

        # Register tools
        if manifest.tools:
            for tool_def in manifest.tools:
                tool_def["_plugin"] = name
                self._tools.append(tool_def)

        # Also check for tools.py
        tools_path = os.path.join(plugin_path, "tools.py")
        if os.path.exists(tools_path):
            try:
                tools_spec = importlib.util.spec_from_file_location(
                    f"plugins.{name}.tools", tools_path)
                if tools_spec and tools_spec.loader:
                    tools_mod = importlib.util.module_from_spec(tools_spec)
                    tools_spec.loader.exec_module(tools_mod)
                    # Look for TOOLS list
                    if hasattr(tools_mod, "TOOLS"):
                        for t in tools_mod.TOOLS:
                            t["_plugin"] = name
                            self._tools.append(t)
            except Exception as e:
                logger.warning("[plugin] '%s' tools.py load failed: %s", name, e)

        logger.info("[plugin] loaded '%s' v%s (%d hooks, %d tools)",
                    name, manifest.version,
                    len(manifest.hooks), len(manifest.tools))
        return plugin

    def unload(self, name: str):
        """Unload a plugin."""
        plugin = self._plugins.pop(name, None)
        if not plugin:
            return

        # Remove hooks
        for hook_name, handlers in self._hooks.items():
            self._hooks[hook_name] = [
                h for h in handlers
                if not (hasattr(h, '__module__') and
                        h.__module__ == f"plugins.{name}")
            ]

        # Remove tools
        self._tools = [t for t in self._tools if t.get("_plugin") != name]

        # Remove from sys.modules
        for key in list(sys.modules.keys()):
            if key.startswith(f"plugins.{name}"):
                del sys.modules[key]

        logger.info("[plugin] unloaded '%s'", name)

    async def emit(self, hook_name: str, **kwargs):
        """Emit a hook event to all registered handlers.

        Handlers are called in registration order. Errors in one handler
        don't prevent others from running.
        """
        handlers = self._hooks.get(hook_name, [])
        for handler in handlers:
            try:
                import asyncio
                if asyncio.iscoroutinefunction(handler):
                    await handler(**kwargs)
                else:
                    handler(**kwargs)
            except Exception as e:
                logger.warning("[plugin] hook %s handler error: %s",
                               hook_name, e)

    def emit_sync(self, hook_name: str, **kwargs):
        """Synchronous hook emission for non-async contexts."""
        handlers = self._hooks.get(hook_name, [])
        for handler in handlers:
            try:
                import asyncio
                if asyncio.iscoroutinefunction(handler):
                    # Skip async handlers in sync context
                    logger.debug("[plugin] skipping async handler for %s in sync context",
                                 hook_name)
                    continue
                handler(**kwargs)
            except Exception as e:
                logger.warning("[plugin] hook %s handler error: %s",
                               hook_name, e)

    def get_tools(self) -> list[dict]:
        """Return all tools registered by plugins."""
        return list(self._tools)

    def list_plugins(self) -> list[dict]:
        """Return info about all loaded plugins (for dashboard)."""
        return [
            {
                "name": p.manifest.name,
                "version": p.manifest.version,
                "description": p.manifest.description,
                "hooks": p.manifest.hooks,
                "tools": len(p.manifest.tools),
                "enabled": p.manifest.enabled,
                "loaded_at": p.loaded_at,
            }
            for p in self._plugins.values()
        ]

    def _parse_manifest(self, plugin_path: str, name: str) -> PluginManifest:
        """Parse plugin manifest.yaml."""
        manifest_path = os.path.join(plugin_path, "manifest.yaml")
        if os.path.exists(manifest_path):
            try:
                import yaml
                with open(manifest_path) as f:
                    data = yaml.safe_load(f) or {}
                return PluginManifest(
                    name=data.get("name", name),
                    version=data.get("version", "0.1.0"),
                    description=data.get("description", ""),
                    author=data.get("author", ""),
                    hooks=data.get("hooks", []),
                    tools=data.get("tools", []),
                    config_schema=data.get("config", {}),
                    enabled=data.get("enabled", True),
                )
            except Exception as e:
                logger.warning("[plugin] manifest parse error for '%s': %s", name, e)

        # Fallback: try manifest.json
        json_path = os.path.join(plugin_path, "manifest.json")
        if os.path.exists(json_path):
            try:
                with open(json_path) as f:
                    data = json.load(f)
                return PluginManifest(
                    name=data.get("name", name),
                    version=data.get("version", "0.1.0"),
                    description=data.get("description", ""),
                    hooks=data.get("hooks", []),
                    tools=data.get("tools", []),
                    enabled=data.get("enabled", True),
                )
            except Exception as e:
                logger.warning("[plugin] json manifest parse error for '%s': %s",
                               name, e)

        # Minimal manifest from directory name
        return PluginManifest(name=name)


# ── Singleton access ──
_plugin_manager: Optional[PluginManager] = None


def get_plugin_manager() -> PluginManager:
    """Get or create the global PluginManager singleton."""
    global _plugin_manager
    if _plugin_manager is None:
        _plugin_manager = PluginManager()
        _plugin_manager.load_all()
    return _plugin_manager
