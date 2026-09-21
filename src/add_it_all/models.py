"""
The unified record produced by aggregating every plugin's result for one
identifier. Deliberately loose on a per-field basis: a missing or
errored plugin degrades that one field, not the whole record.

The identifier is whatever the queried plugins look up -- a person for
Okta/Jira/Jamf, an application or vendor for CAIRO. Named `UnifiedRecord`
rather than `UnifiedUserRecord` (renamed 2026-09-04) precisely because not
every connector here is person-scoped, and the old name taught the wrong
mental model to anyone adding one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from add_it_all.plugins.base import ConnectorResult


@dataclass
class UnifiedRecord:
    identifier: str
    results: dict[str, ConnectorResult] = field(default_factory=dict)

    @classmethod
    def from_results(cls, identifier: str, results: list[ConnectorResult]) -> "UnifiedRecord":
        return cls(identifier=identifier, results={r.plugin_name: r for r in results})

    def field_for(self, plugin_name: str) -> dict[str, Any] | None:
        """Data payload for one plugin, or None if that plugin didn't run/errored."""
        result = self.results.get(plugin_name)
        if result is None or not result.ok:
            return None
        return result.data

    def errors(self) -> dict[str, str]:
        """Map of plugin_name -> error message, for plugins that failed."""
        return {
            name: r.error
            for name, r in self.results.items()
            if not r.ok and r.error is not None
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "results": {
                name: {
                    "data": r.data,
                    "properties": r.properties,
                    "tags": r.tags,
                    "fetched_at": r.fetched_at.isoformat(),
                    "error": r.error,
                }
                for name, r in self.results.items()
            },
        }
