"""
Secret scrubbing for plugin error strings.

The plugin contract (base.py rule: `fetch()` must never raise) means every
ordinary failure becomes `ConnectorResult(error=str(exc))`. That string is
then persisted to the SQLite cache and printed to the terminal, so an
exception carrying a URL, an auth header, or a token turns into a durable
plaintext leak. `safe_error()` is the scrubbing seam every connector runs
its exceptions through.

Run just this stage:  pytest -m plugin_framework
"""

from __future__ import annotations

import pytest

from lookup_cli.redaction import safe_error

pytestmark = pytest.mark.plugin_framework

REDACTED = "***"


def test_ordinary_message_passes_through_unchanged():
    assert safe_error(RuntimeError("boom")) == "boom"


def test_accepts_a_bare_string_as_well_as_an_exception():
    assert safe_error("boom") == "boom"


def test_keeps_the_message_useful_for_debugging():
    """Scrubbing must not destroy the diagnostic signal."""
    scrubbed = safe_error("404 Not Found for https://acme.okta.com/api/v1/users/jdoe")
    assert "404" in scrubbed
    assert "acme.okta.com" in scrubbed
    assert "jdoe" in scrubbed


# --- Authorization headers ---------------------------------------------------


def test_bearer_token_is_redacted():
    scrubbed = safe_error("401 with header Authorization: Bearer abc123def456ghi")
    assert "abc123def456ghi" not in scrubbed
    assert REDACTED in scrubbed


def test_okta_ssws_token_is_redacted():
    scrubbed = safe_error("Authorization: SSWS 00aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890")
    assert "00aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890" not in scrubbed
    assert REDACTED in scrubbed


def test_basic_auth_header_is_redacted():
    scrubbed = safe_error("Authorization: Basic amRvZTpzZWNyZXR0b2tlbg==")
    assert "amRvZTpzZWNyZXR0b2tlbg==" not in scrubbed


# --- URLs --------------------------------------------------------------------


def test_token_in_query_string_is_redacted():
    scrubbed = safe_error("GET https://api.example.com/v1/x?api_key=supersecretvalue&page=2")
    assert "supersecretvalue" not in scrubbed
    assert "page=2" in scrubbed, "non-secret params should survive"


def test_url_userinfo_credentials_are_redacted():
    scrubbed = safe_error("failed: https://jdoe:hunter2pass@jira.example.com/rest/api/2/search")
    assert "hunter2pass" not in scrubbed
    assert "jira.example.com" in scrubbed


def test_jwt_is_redacted():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJqZG9lIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g"
    scrubbed = safe_error(f"auth failed with assertion {jwt}")
    assert jwt not in scrubbed


# --- Values pulled from the environment --------------------------------------


def test_secret_valued_env_vars_are_redacted_wherever_they_appear(monkeypatch):
    """The catch-all: if the literal token text shows up, scrub it."""
    monkeypatch.setenv("OKTA_API_TOKEN", "s3cr3t-okta-token-value")
    scrubbed = safe_error("upstream rejected s3cr3t-okta-token-value")
    assert "s3cr3t-okta-token-value" not in scrubbed
    assert REDACTED in scrubbed


def test_short_env_values_are_not_redacted(monkeypatch):
    """LOOKUP_CLI_MOCK_JAMF=1 must not turn every '1' in a message into ***."""
    monkeypatch.setenv("LOOKUP_CLI_MOCK_JAMF", "1")
    assert safe_error("timeout after 1 retry") == "timeout after 1 retry"


def test_path_valued_vars_are_not_treated_as_secrets(monkeypatch):
    """A _PATH var points at a key; it is not itself a secret."""
    monkeypatch.setenv("VENDOR_PRIVATE_KEY_PATH", "/tmp/keys/vendor.pem")
    scrubbed = safe_error("could not read /tmp/keys/vendor.pem")
    assert "/tmp/keys/vendor.pem" in scrubbed


def test_non_secret_env_vars_are_left_alone(monkeypatch):
    monkeypatch.setenv("OKTA_ORG_URL", "https://acme.okta.com")
    scrubbed = safe_error("connection refused to https://acme.okta.com")
    assert "acme.okta.com" in scrubbed


def test_explicitly_supplied_secrets_are_redacted():
    scrubbed = safe_error("bad credential zzz-inline-secret", secrets=["zzz-inline-secret"])
    assert "zzz-inline-secret" not in scrubbed


def test_empty_and_none_secrets_are_ignored_safely(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "")
    assert safe_error("plain message", secrets=["", None]) == "plain message"
