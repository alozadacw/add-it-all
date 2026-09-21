"""
Template connector plugin.

To build a real connector (e.g. Jamf):
    1. Copy this whole `plugins/echo_plugin/` directory to `plugins/jamf_plugin/`.
    2. Rename the package dir `echo_plugin/` -> `jamf_plugin/`.
    3. Rewrite `fetch()` below to call the real API, with your own client
       class so it's easy to swap a mock client for a real one (see
       docs/CONNECTOR_GUIDE.md for the recommended shape).
    4. Update pyproject.toml: project name, entry-point name/target.
    5. Write tests FIRST (tests/test_plugin.py) against a mocked HTTP
       client before writing the real fetch() logic -- this is a
       TDD project.
    6. `pip install -e plugins/jamf_plugin`.
"""

from __future__ import annotations

from add_it_all.plugins.base import ConnectorPlugin, ConnectorResult
from add_it_all.redaction import safe_error


class EchoStandalonePlugin(ConnectorPlugin):
    name = "echo_standalone"

    async def fetch(self, identifier: str) -> ConnectorResult:
        try:
            data = await self._call_backend(identifier)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: never crash aggregation
            # safe_error, never str(exc): this string is persisted to the
            # SQLite cache and printed, and a real connector's exceptions
            # carry URLs and auth headers. See add_it_all/redaction.py.
            return ConnectorResult(
                plugin_name=self.name, identifier=identifier, error=safe_error(exc)
            )

        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data=data,
            tags=["template-plugin"],
        )

    async def _call_backend(self, identifier: str) -> dict:
        # Replace this with a real API call (httpx.AsyncClient). Keep it as
        # its own method so tests can monkeypatch/mock it cleanly, and so
        # swapping mock-mode for real-mode is a one-method change:
        #
        #     if self.mock_mode:
        #         return self._mock_fixture(identifier)
        #     token = self.config.require("ECHO_API_TOKEN")
        #
        return {"echoed": identifier}
