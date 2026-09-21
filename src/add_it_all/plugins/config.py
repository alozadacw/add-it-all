"""
Credential/configuration delivery for connector plugins.

Core builds one `PluginConfig` and injects it into every plugin at
discovery time, rather than each plugin reaching into `os.environ` itself
(decided 2026-08-25; see the Open Decisions Log in docs/STAGES.md). Three
things fall out of that:

* core can ask a plugin whether it is configured, so `plugins list` and
  `lookup` can skip or warn instead of failing mid-fetch;
* tests inject a mapping instead of monkeypatching the environment; and
* `.env` is read once, in one place, with one documented precedence rule.

That last point is load-bearing. `bootstrap.sh` writes credentials into
`.env` and nothing exports them into the shell, so a plugin that only read
`os.environ` would see nothing after a developer followed the README.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

#: Spellings accepted for boolean env vars such as ADD_IT_ALL_MOCK_JAMF.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

DOTENV_FILENAME = ".env"


class MissingCredential(RuntimeError):
    """A plugin asked for a credential that isn't configured."""


class PluginConfig:
    """Read-only view over the credentials available to plugins.

    Construct directly with a mapping in tests; use `from_env()` in
    production code.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str] | None = None):
        self._values: dict[str, str] = dict(values or {})

    @classmethod
    def from_env(cls, dotenv_path: str | Path = DOTENV_FILENAME) -> "PluginConfig":
        """Merge `.env` and the process environment.

        The process environment wins, matching how `Settings` resolves the
        core keys -- one precedence rule for the whole project.
        """
        values: dict[str, str] = {
            key: value for key, value in dotenv_values(dotenv_path).items() if value is not None
        }
        values.update(os.environ)
        return cls(values)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._values.get(key, default)

    def require(self, key: str) -> str:
        """Return `key`'s value, or raise if it is absent or blank.

        Blank counts as missing: `.env.example` ships `OKTA_API_TOKEN=`, so
        an unfilled placeholder must not read as a configured credential.
        """
        value = self._values.get(key, "")
        if not value.strip():
            raise MissingCredential(
                f"{key} is not set. Add it to your .env or export it; "
                f"see .env.example for the expected name."
            )
        return value

    def has(self, key: str) -> bool:
        return bool(self._values.get(key, "").strip())

    def flag(self, key: str) -> bool:
        """Interpret `key` as a boolean toggle (e.g. ADD_IT_ALL_MOCK_JAMF)."""
        return self._values.get(key, "").strip().lower() in _TRUTHY

    def __repr__(self) -> str:
        # Names only, never values -- this can surface in a traceback.
        return f"PluginConfig(keys={sorted(self._values)!r})"
