"""
Secret scrubbing for connector error strings.

Why this exists: the plugin contract requires `fetch()` to swallow ordinary
failures and return `ConnectorResult(error=...)` instead of raising. That
error string is not ephemeral -- `Cache.put()` serialises it into SQLite and
the CLI prints it. An httpx exception routinely carries the request URL, and
a mis-handled one can carry an auth header, so the natural `str(exc)` turns a
transient 401 into a durable plaintext credential on disk.

Every connector should run exceptions through `safe_error()` rather than
calling `str(exc)` directly. See docs/CONNECTOR_GUIDE.md step 4.

This is deliberately defence-in-depth, not a licence to be careless: the
first line of defence is still never putting a credential somewhere it can
be interpolated into a message.
"""

from __future__ import annotations

import os
import re
from typing import Iterable

REDACTED = "***"

#: Env values shorter than this are ignored as redaction targets -- scrubbing
#: every occurrence of a value like "1" (LOOKUP_CLI_MOCK_JAMF=1) would shred
#: unrelated text and make errors unreadable.
_MIN_ENV_SECRET_LEN = 8

#: Env var names whose *values* are secrets.
_SECRET_NAME_RE = re.compile(
    r"(?i)(token|secret|passwd|password|api[_-]?key|private[_-]?key|credential)"
)

#: ...except these suffixes, which name a location or identity rather than a
#: secret. A var like `VENDOR_PRIVATE_KEY_PATH` names a filesystem path worth keeping visible
#: in a "could not read" error.
_SECRET_NAME_EXCEPTIONS_RE = re.compile(r"(?i)_(path|file|filename|dir|url|uri|id|name|email)$")

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # `Authorization: Bearer <cred>`, Okta's `SSWS <cred>`, `Basic <cred>`.
    (
        re.compile(r"(?i)\b(bearer|ssws|basic|token|apikey)(\s+)([A-Za-z0-9._~+/=-]{6,})"),
        r"\1\2" + REDACTED,
    ),
    # Credentials embedded in a URL: https://user:pass@host
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@"),
        r"\g<1>" + REDACTED + ":" + REDACTED + "@",
    ),
    # Secrets in a query string, preserving the parameter name and any
    # non-secret parameters around it.
    (
        re.compile(
            r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|password|secret"
            r"|signature|sig|key)=)[^&\s\"']+"
        ),
        r"\1" + REDACTED,
    ),
    # A bare JWT (server-to-server assertions, among others).
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
        REDACTED,
    ),
)


def _env_secret_values() -> list[str]:
    """Literal secret values visible in the environment, longest first."""
    values = [
        value
        for name, value in os.environ.items()
        if _SECRET_NAME_RE.search(name)
        and not _SECRET_NAME_EXCEPTIONS_RE.search(name)
        and len(value.strip()) >= _MIN_ENV_SECRET_LEN
    ]
    return values


def redact(text: str, *, secrets: Iterable[str | None] = ()) -> str:
    """Scrub known and pattern-matched secrets out of `text`."""
    # Literal values first (most specific), longest first so that a secret
    # which contains another secret as a substring is replaced whole.
    literals = [s for s in secrets if s] + _env_secret_values()
    for literal in sorted(set(literals), key=len, reverse=True):
        text = text.replace(literal, REDACTED)

    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)

    return text


def safe_error(exc: BaseException | str, *, secrets: Iterable[str | None] = ()) -> str:
    """Render an exception as a message safe to cache, log, and display.

    Pass any credential the caller holds but that is not exposed as an
    obviously-named env var via `secrets`.

        except httpx.HTTPError as exc:
            return ConnectorResult(..., error=safe_error(exc))
    """
    return redact(str(exc), secrets=secrets)
