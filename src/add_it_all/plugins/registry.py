"""
Plugin discovery via Python entry points.

Any installed package that registers a class under the
`add_it_all.plugins` entry-point group is automatically picked up --
no import list to maintain in core code.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Iterator

from add_it_all.plugins.base import ConnectorPlugin
from add_it_all.plugins.config import PluginConfig

ENTRY_POINT_GROUP = "add_it_all.plugins"


class PluginLoadError(RuntimeError):
    """Raised when a registered entry point doesn't satisfy the plugin contract."""


def discover_plugins(config: PluginConfig | None = None) -> dict[str, ConnectorPlugin]:
    """Load and instantiate every registered plugin.

    `config` is built once here and injected into every plugin, so each
    connector does not read the environment for itself and core can tell
    which plugins are actually configured.

    Returns a dict keyed by plugin `.name`. Raises PluginLoadError with
    a clear message if an entry point doesn't resolve to a valid
    ConnectorPlugin subclass -- fail loud at startup, not silently at
    lookup time.
    """
    if config is None:
        config = PluginConfig.from_env()

    plugins: dict[str, ConnectorPlugin] = {}
    for ep in _iter_entry_points():
        try:
            plugin_cls = ep.load()
        except Exception as exc:  # noqa: BLE001 - re-raised with context below
            raise PluginLoadError(
                f"Failed to load plugin entry point '{ep.name}' ({ep.value}): {exc}"
            ) from exc

        if not (isinstance(plugin_cls, type) and issubclass(plugin_cls, ConnectorPlugin)):
            raise PluginLoadError(
                f"Entry point '{ep.name}' ({ep.value}) does not resolve to a "
                f"ConnectorPlugin subclass."
            )

        try:
            instance = plugin_cls(config)
        except TypeError as exc:
            raise PluginLoadError(
                f"Plugin '{ep.name}' ({ep.value}) could not be constructed with a "
                f"PluginConfig. Connector plugins must accept one via "
                f"ConnectorPlugin.__init__ -- if you overrode __init__, call "
                f"super().__init__(config). Underlying error: {exc}"
            ) from exc

        if not getattr(instance, "name", None):
            raise PluginLoadError(
                f"Plugin loaded from entry point '{ep.name}' has no `name` attribute."
            )
        plugins[instance.name] = instance

    return plugins


def _iter_entry_points() -> Iterator:
    eps = entry_points()
    # importlib.metadata's select() API (3.10+) vs. dict-like fallback
    if hasattr(eps, "select"):
        return iter(eps.select(group=ENTRY_POINT_GROUP))
    return iter(eps.get(ENTRY_POINT_GROUP, []))  # type: ignore[union-attr]
