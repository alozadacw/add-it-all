from __future__ import annotations

from datetime import timedelta

import typer
from rich.console import Console
from rich.table import Table

from lookup_cli.cache import Cache
from lookup_cli.config import get_settings
from lookup_cli.plugins.registry import PluginLoadError, discover_plugins

plugins_app = typer.Typer(help="Inspect installed connector plugins.")
cache_app = typer.Typer(help="Inspect and empty the local result cache.")

console = Console()


def build_app(plugins: dict | None = None) -> typer.Typer:
    """Assemble the CLI, mounting every plugin's own sub-app.

    Per-service subcommands live in their plugin packages and are mounted
    here generically, so adding a connector never edits this file (ground
    rule 2 / Stage 8). Pass `plugins` to build an app over a fixed set --
    used by tests.
    """
    application = typer.Typer(
        help="Unified lookup across Okta, Jira, Jamf, allwhere, and more."
    )
    application.add_typer(plugins_app, name="plugins")
    application.add_typer(cache_app, name="cache")

    if plugins is None:
        try:
            plugins = discover_plugins()
        except PluginLoadError:
            # Don't let one broken entry point stop the CLI from loading --
            # `plugins list` is how the user diagnoses exactly this, and it
            # reports the error properly.
            plugins = {}

    for name, plugin in sorted(plugins.items()):
        sub_app = plugin.cli()
        if sub_app is not None:
            application.add_typer(sub_app, name=name)

    return application


@plugins_app.command("list")
def list_plugins() -> None:
    """List every connector plugin currently discoverable via entry points."""
    try:
        plugins = discover_plugins()
    except PluginLoadError as exc:
        console.print(f"[red]Plugin load error:[/red] {exc}")
        raise typer.Exit(code=1)

    table = Table(title="Installed connector plugins")
    table.add_column("name")
    table.add_column("class")
    table.add_column("status")
    for name, plugin in sorted(plugins.items()):
        if plugin.mock_mode:
            status = "[yellow]mock[/yellow]"
        elif plugin.configured:
            status = "[green]configured[/green]"
        else:
            missing = ", ".join(k for k in plugin.required_credentials if not plugin.config.has(k))
            status = f"[red]missing:[/red] {missing}"
        table.add_row(name, type(plugin).__qualname__, status)
    console.print(table)


def _open_cache() -> Cache:
    settings = get_settings()
    return Cache(settings.cache_db_path, default_ttl=timedelta(seconds=settings.cache_ttl_seconds))


@cache_app.command("path")
def cache_path() -> None:
    """Print the path of the cache database currently in use."""
    # typer.echo, not console.print: this is machine-readable output that
    # may be piped, so it must not be wrapped or markup-interpreted.
    typer.echo(str(get_settings().cache_db_path))


@cache_app.command("clear")
def cache_clear() -> None:
    """Delete every cached result.

    The cache stores employee PII in plaintext, so this is the supported
    way to empty it -- no need to know the on-disk path.
    """
    removed = _open_cache().clear()
    console.print(f"Cleared {removed} cached entr{'y' if removed == 1 else 'ies'}.")


@cache_app.command("purge")
def cache_purge() -> None:
    """Delete only cached results that are already past their TTL."""
    removed = _open_cache().purge_expired()
    console.print(f"Purged {removed} expired entr{'y' if removed == 1 else 'ies'}.")


app = build_app()


if __name__ == "__main__":
    app()
