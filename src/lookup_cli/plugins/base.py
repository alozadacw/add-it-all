"""
The plugin contract.

Every connector (Okta, Jira, Jamf, allwhere, or any future service)
implements `ConnectorPlugin` and returns a `ConnectorResult`. The core
CLI, cache, and aggregation logic depend ONLY on this interface -- never
on any specific service's API shape. This is what makes new connectors
pluggable without touching core code.

To add a new service:
    1. Create a package (see plugins/echo_plugin for the reference shape).
    2. Implement ConnectorPlugin.fetch() -- note it is `async def`.
    3. Register it under the `lookup_cli.plugins` entry-point group in
       that package's pyproject.toml.
    4. `pip install -e .` the plugin package. Done -- no core changes.

See docs/CONNECTOR_GUIDE.md for the full walkthrough.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from lookup_cli.plugins.config import PluginConfig


@dataclass
class ConnectorResult:
    """Uniform shape returned by every plugin, regardless of backend.

    `data` holds the fields the plugin's docs promise (e.g. Okta's
    `status`). `properties` and `tags` are open-ended extension points --
    a plugin can attach arbitrary extra fields there without requiring
    any change to this dataclass or to core aggregation logic.
    """

    plugin_name: str
    identifier: str
    data: dict[str, Any] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class ConnectorPlugin(ABC):
    """Base class every connector plugin must subclass."""

    #: Short, stable, lowercase identifier used in cache keys, CLI
    #: subcommands, and entry-point registration (e.g. "okta").
    name: str

    #: Env var names this plugin needs for real (non-mock) operation. Used
    #: by `configured` so core can report an unconfigured plugin up front
    #: rather than failing mid-fetch.
    required_credentials: tuple[str, ...] = ()

    def __init__(self, config: PluginConfig | None = None):
        #: Injected by the registry. Falls back to the environment so a
        #: plugin remains constructible standalone (handy in a REPL).
        self.config = config if config is not None else PluginConfig.from_env()

    @property
    def mock_mode(self) -> bool:
        """Whether this plugin should serve fixtures instead of calling out.

        Follows the `LOOKUP_CLI_MOCK_<PLUGIN>` convention already used in
        `.env.example`, so Jamf/allwhere can be built before their
        credentials exist.
        """
        return self.config.flag(f"LOOKUP_CLI_MOCK_{self.name.upper()}")

    @property
    def configured(self) -> bool:
        """True when this plugin can actually run."""
        if self.mock_mode:
            return True
        return all(self.config.has(key) for key in self.required_credentials)

    def cli(self) -> Any | None:
        """Optional `typer.Typer` sub-app, mounted at `lookup-cli <name> ...`.

        Return None (the default) for no subcommands. Defining this in the
        plugin package is what keeps `src/lookup_cli/cli.py` free of
        per-service wiring: adding a connector never edits core, which is
        the claim Stage 8 exists to verify.

        Typed loosely so the plugin contract doesn't hard-depend on typer.
        """
        return None

    @abstractmethod
    async def fetch(self, identifier: str) -> ConnectorResult:
        """Look up `identifier` (e.g. username or email) in this service.

        Async so that Stage 7 can aggregate every plugin concurrently with
        `asyncio.gather` -- serially, a lookup would cost the sum of every
        service's round trip.

        Must NOT raise on ordinary failure conditions (not found, auth
        error, timeout, etc.) -- catch those and return a ConnectorResult
        with `error` set instead, so one failing plugin never breaks
        aggregation across the other plugins. Only truly unexpected
        programming errors should propagate.

        Build that error string with `safe_error(exc)` from
        `lookup_cli.redaction`, never `str(exc)`: it is persisted to the
        cache and printed, and client exceptions carry URLs and auth
        headers.
        """
        raise NotImplementedError
