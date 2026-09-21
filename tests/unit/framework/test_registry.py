"""
Stage 0 acceptance tests: the plugin framework.

Run just this stage:  pytest -m plugin_framework
"""

import inspect

import pytest

from add_it_all.plugins.base import ConnectorPlugin, ConnectorResult
from add_it_all.plugins.config import PluginConfig
from add_it_all.plugins.registry import PluginLoadError, discover_plugins

pytestmark = pytest.mark.plugin_framework


def test_builtin_echo_plugin_is_discovered():
    plugins = discover_plugins()
    assert "echo" in plugins
    assert isinstance(plugins["echo"], ConnectorPlugin)


async def test_echo_plugin_fetch_returns_connector_result():
    plugins = discover_plugins()
    result = await plugins["echo"].fetch("jdoe")
    assert isinstance(result, ConnectorResult)
    assert result.plugin_name == "echo"
    assert result.identifier == "jdoe"
    assert result.data == {"echoed": "jdoe"}
    assert result.ok is True


def test_fetch_is_a_coroutine_function():
    """Pins the async contract: Stage 7 aggregates with asyncio.gather, so a
    plugin that defines a plain `def fetch` would silently return a
    non-awaited value rather than a ConnectorResult."""
    assert inspect.iscoroutinefunction(discover_plugins()["echo"].fetch)


def test_discovered_plugins_receive_the_injected_config():
    config = PluginConfig({"SENTINEL": "injected"})
    plugins = discover_plugins(config)
    assert plugins["echo"].config.get("SENTINEL") == "injected"


def test_discover_plugins_defaults_to_environment_config():
    assert discover_plugins()["echo"].config is not None


def test_invalid_plugin_class_raises_plugin_load_error(monkeypatch):
    """An entry point that doesn't resolve to a ConnectorPlugin subclass
    must fail loudly at load time, not silently at lookup time."""

    class FakeEntryPoint:
        name = "broken"
        value = "not.a.real.module:NotAPlugin"

        def load(self):
            class NotAPlugin:  # does not subclass ConnectorPlugin
                pass

            return NotAPlugin

    monkeypatch.setattr(
        "add_it_all.plugins.registry._iter_entry_points",
        lambda: iter([FakeEntryPoint()]),
    )

    with pytest.raises(PluginLoadError):
        discover_plugins()


def test_plugin_rejecting_config_raises_an_actionable_plugin_load_error(monkeypatch):
    """A connector that overrides __init__ without calling super() is the most
    likely way to break config injection. The message must say how to fix it,
    not just surface a bare TypeError from deep in the registry."""

    class FakeEntryPoint:
        name = "legacy"
        value = "not.a.real.module:LegacyPlugin"

        def load(self):
            class LegacyPlugin(ConnectorPlugin):
                name = "legacy"

                def __init__(self):  # no config parameter
                    pass

                async def fetch(self, identifier: str) -> ConnectorResult:
                    return ConnectorResult(plugin_name="legacy", identifier=identifier)

            return LegacyPlugin

    monkeypatch.setattr(
        "add_it_all.plugins.registry._iter_entry_points",
        lambda: iter([FakeEntryPoint()]),
    )

    with pytest.raises(PluginLoadError) as excinfo:
        discover_plugins()

    message = str(excinfo.value)
    assert "legacy" in message
    assert "super().__init__(config)" in message


def test_plugin_missing_name_attribute_raises_plugin_load_error(monkeypatch):
    class FakeEntryPoint:
        name = "nameless"
        value = "not.a.real.module:Nameless"

        def load(self):
            class Nameless(ConnectorPlugin):
                name = ""  # falsy -> should be rejected

                async def fetch(self, identifier: str) -> ConnectorResult:
                    return ConnectorResult(plugin_name="nameless", identifier=identifier)

            return Nameless

    monkeypatch.setattr(
        "add_it_all.plugins.registry._iter_entry_points",
        lambda: iter([FakeEntryPoint()]),
    )

    with pytest.raises(PluginLoadError):
        discover_plugins()
