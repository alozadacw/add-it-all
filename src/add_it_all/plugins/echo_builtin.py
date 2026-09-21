"""
Reference plugin used to prove the plugin framework works end-to-end
(Stage 0) before any real service connector exists. Also doubles as the
minimal example new plugin authors can copy.
"""

from __future__ import annotations

from add_it_all.plugins.base import ConnectorPlugin, ConnectorResult


class EchoPlugin(ConnectorPlugin):
    name = "echo"

    async def fetch(self, identifier: str) -> ConnectorResult:
        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data={"echoed": identifier},
            properties={},
            tags=["stage-0-reference"],
        )
