"""
Plugin-supplied CLI subcommands.

Ground rule 2 says a connector task must not touch core. Wiring each
service's subcommand into `src/add_it_all/cli.py` by hand would break that
for every connector, so core mounts whatever sub-app a plugin returns from
`cli()` instead. One generic core change, then never again -- which is the
extensibility claim Stage 8 checks.

Run just this stage:  pytest -m plugin_framework
"""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from add_it_all.cli import build_app
from add_it_all.plugins.base import ConnectorPlugin, ConnectorResult
from add_it_all.plugins.config import PluginConfig
from add_it_all.plugins.registry import PluginLoadError

pytestmark = pytest.mark.plugin_framework

runner = CliRunner()


class _WithCli(ConnectorPlugin):
    name = "widget"

    async def fetch(self, identifier: str) -> ConnectorResult:
        return ConnectorResult(plugin_name=self.name, identifier=identifier)

    def cli(self):
        sub = typer.Typer(help="Widget lookups.")

        @sub.command("status")
        def status(identifier: str) -> None:
            typer.echo(f"widget-status:{identifier}")

        return sub


class _WithoutCli(ConnectorPlugin):
    name = "plain"

    async def fetch(self, identifier: str) -> ConnectorResult:
        return ConnectorResult(plugin_name=self.name, identifier=identifier)


def test_plugin_subcommand_is_mounted_under_the_plugin_name():
    app = build_app({"widget": _WithCli(PluginConfig({}))})
    result = runner.invoke(app, ["widget", "status", "jdoe"])
    assert result.exit_code == 0
    assert "widget-status:jdoe" in result.stdout


def test_plugin_without_a_cli_is_skipped_not_an_error():
    app = build_app({"plain": _WithoutCli(PluginConfig({}))})
    result = runner.invoke(app, ["plugins", "list"])
    assert result.exit_code == 0


def test_core_commands_still_work_alongside_plugin_subcommands():
    app = build_app({"widget": _WithCli(PluginConfig({}))})
    assert runner.invoke(app, ["cache", "path"]).exit_code == 0


def test_broken_plugin_discovery_does_not_prevent_the_cli_from_starting(monkeypatch):
    """`add-it-all plugins list` is how you diagnose a broken plugin -- it must
    still run when discovery raises, rather than the whole CLI failing to load."""

    def boom(*args, **kwargs):
        raise PluginLoadError("entry point exploded")

    monkeypatch.setattr("add_it_all.cli.discover_plugins", boom)

    app = build_app()  # must not raise
    result = runner.invoke(app, ["plugins", "list"])
    assert result.exit_code == 1
    assert "Plugin load error" in result.stdout
