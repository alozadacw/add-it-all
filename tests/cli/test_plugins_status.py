"""
`plugins list` reports whether each connector can actually run.

This is the payoff of injecting config rather than having each plugin read
the environment itself: core can distinguish configured / mock / missing
up front, instead of every plugin failing separately mid-lookup.

Run just this stage:  pytest -m plugin_framework
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from add_it_all.cli import app
from add_it_all.plugins.base import ConnectorPlugin, ConnectorResult
from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.plugin_framework

runner = CliRunner()


class _NeedsToken(ConnectorPlugin):
    name = "needy"
    required_credentials = ("NEEDY_API_TOKEN", "NEEDY_ORG_URL")

    async def fetch(self, identifier: str) -> ConnectorResult:
        return ConnectorResult(plugin_name=self.name, identifier=identifier)


def _patch_discovery(monkeypatch, plugin) -> None:
    monkeypatch.setattr(
        "add_it_all.cli.discover_plugins",
        lambda *args, **kwargs: {plugin.name: plugin},
    )


def test_configured_plugin_is_reported_as_configured(monkeypatch):
    plugin = _NeedsToken(PluginConfig({"NEEDY_API_TOKEN": "x", "NEEDY_ORG_URL": "y"}))
    _patch_discovery(monkeypatch, plugin)

    result = runner.invoke(app, ["plugins", "list"])

    assert result.exit_code == 0
    assert "configured" in result.stdout


def test_unconfigured_plugin_names_the_missing_credentials(monkeypatch):
    """Naming them is the point -- "not configured" alone sends the operator
    hunting through .env.example."""
    plugin = _NeedsToken(PluginConfig({"NEEDY_API_TOKEN": "x"}))
    _patch_discovery(monkeypatch, plugin)

    result = runner.invoke(app, ["plugins", "list"])

    assert result.exit_code == 0
    assert "missing" in result.stdout
    assert "NEEDY_ORG_URL" in result.stdout


def test_mock_mode_plugin_is_reported_as_mock(monkeypatch):
    plugin = _NeedsToken(PluginConfig({"ADD_IT_ALL_MOCK_NEEDY": "1"}))
    _patch_discovery(monkeypatch, plugin)

    result = runner.invoke(app, ["plugins", "list"])

    assert result.exit_code == 0
    assert "mock" in result.stdout


def test_credential_values_are_never_printed(monkeypatch):
    plugin = _NeedsToken(
        PluginConfig({"NEEDY_API_TOKEN": "s3cr3t-token-value", "NEEDY_ORG_URL": "y"})
    )
    _patch_discovery(monkeypatch, plugin)

    result = runner.invoke(app, ["plugins", "list"])

    assert "s3cr3t-token-value" not in result.stdout
