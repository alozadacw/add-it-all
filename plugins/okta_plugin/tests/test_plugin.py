"""
Stage 2 acceptance tests: the Okta connector.

All HTTP is mocked with respx -- no test touches the real Okta org. Every
credential here is obviously fake, per docs/CONTRIBUTING.md.

Run just this stage:  pytest -m okta
"""

from __future__ import annotations

import httpx
import pytest
import respx
from okta_plugin.plugin import OktaPlugin

from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.okta

ORG_URL = "https://acme.okta.com"
USERS_URL = f"{ORG_URL}/api/v1/users"

CONFIG = PluginConfig(
    {
        "OKTA_ORG_URL": ORG_URL,
        "OKTA_API_TOKEN": "not-a-real-token",
    }
)


def _okta_user(status: str = "ACTIVE") -> dict:
    """A trimmed but realistically-shaped Okta user payload."""
    return {
        "id": "00u1abcdefGHIJKLmno7",
        "status": status,
        "created": "2024-03-01T10:00:00.000Z",
        "activated": "2024-03-01T10:05:00.000Z",
        "statusChanged": "2025-11-02T09:00:00.000Z",
        "lastLogin": "2026-08-20T14:31:00.000Z",
        "profile": {
            "firstName": "Jane",
            "lastName": "Doe",
            "email": "jdoe@example.com",
            "login": "jdoe@example.com",
        },
    }


def _plugin(config: PluginConfig = CONFIG) -> OktaPlugin:
    return OktaPlugin(config)


# --- Happy path ---------------------------------------------------------------


@respx.mock
async def test_active_user_returns_status_and_profile():
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_okta_user()))

    result = await _plugin().fetch("jdoe")

    assert result.ok
    assert result.data["found"] is True
    assert result.data["status"] == "ACTIVE"
    assert result.data["email"] == "jdoe@example.com"
    assert result.data["display_name"] == "Jane Doe"
    assert "active" in result.tags


@respx.mock
async def test_optional_detail_lands_in_properties_not_data():
    """Per the guide: don't grow `data`'s schema for extra fields."""
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_okta_user()))

    result = await _plugin().fetch("jdoe")

    assert result.properties["okta_id"] == "00u1abcdefGHIJKLmno7"
    assert result.properties["last_login"] == "2026-08-20T14:31:00.000Z"


@pytest.mark.parametrize("status", ["SUSPENDED", "DEPROVISIONED", "LOCKED_OUT", "PASSWORD_EXPIRED"])
@respx.mock
async def test_non_active_statuses_are_reported_and_tagged_inactive(status):
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_okta_user(status)))

    result = await _plugin().fetch("jdoe")

    assert result.ok, "a suspended account is a successful lookup, not a failure"
    assert result.data["status"] == status
    assert "inactive" in result.tags


@respx.mock
async def test_missing_profile_fields_do_not_crash():
    payload = {"id": "00u1", "status": "ACTIVE", "profile": {}}
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=payload))

    result = await _plugin().fetch("jdoe")

    assert result.ok
    assert result.data["display_name"] is None


# --- Request shape ------------------------------------------------------------


@respx.mock
async def test_sends_the_ssws_authorization_header():
    route = respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_okta_user())
    )

    await _plugin().fetch("jdoe")

    assert route.calls.last.request.headers["Authorization"] == "SSWS not-a-real-token"


@respx.mock
async def test_identifier_is_url_encoded():
    """Identifiers are user input: an email is normal, `../` is not."""
    route = respx.get(f"{USERS_URL}/jdoe%40example.com").mock(
        return_value=httpx.Response(200, json=_okta_user())
    )

    await _plugin().fetch("jdoe@example.com")

    assert route.called


@respx.mock
async def test_path_traversal_in_identifier_cannot_escape_the_users_endpoint():
    respx.get(url__startswith=USERS_URL).mock(return_value=httpx.Response(404))

    result = await _plugin().fetch("../../api/v1/apps")

    assert result.ok  # treated as an ordinary not-found
    assert result.data["found"] is False


@respx.mock
async def test_trailing_slash_on_org_url_is_tolerated():
    config = PluginConfig({"OKTA_ORG_URL": f"{ORG_URL}/", "OKTA_API_TOKEN": "not-a-real-token"})
    route = respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_okta_user())
    )

    await _plugin(config).fetch("jdoe")

    assert route.called


# --- Failure modes: fetch() must never raise ----------------------------------


@respx.mock
async def test_user_not_found_is_a_successful_lookup_with_found_false():
    """Design decision (documented in the plugin docstring): "no Okta account"
    is a real answer for an offboarding lookup, not a connector failure. An
    `error=` would make UnifiedRecord.field_for() return None and be
    indistinguishable from "Okta was down"."""
    respx.get(f"{USERS_URL}/ghost").mock(return_value=httpx.Response(404))

    result = await _plugin().fetch("ghost")

    assert result.ok
    assert result.data["found"] is False
    assert result.data["status"] is None
    assert "not-found" in result.tags


@respx.mock
async def test_auth_error_becomes_an_error_result_not_an_exception():
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(401, json={"errorCode": "E0000011"}))

    result = await _plugin().fetch("jdoe")

    assert not result.ok
    assert result.error


@respx.mock
async def test_server_error_becomes_an_error_result():
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(503))

    result = await _plugin().fetch("jdoe")

    assert not result.ok
    assert "503" in result.error


@respx.mock
async def test_rate_limit_becomes_an_error_result():
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(429))

    result = await _plugin().fetch("jdoe")

    assert not result.ok


@respx.mock
async def test_timeout_becomes_an_error_result():
    respx.get(f"{USERS_URL}/jdoe").mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await _plugin().fetch("jdoe")

    assert not result.ok
    assert result.error


@respx.mock
async def test_malformed_json_becomes_an_error_result():
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})
    )

    result = await _plugin().fetch("jdoe")

    assert not result.ok


async def test_missing_credentials_become_an_error_result_not_a_crash():
    result = await OktaPlugin(PluginConfig({})).fetch("jdoe")

    assert not result.ok
    assert "OKTA_ORG_URL" in result.error


# --- Secret hygiene -----------------------------------------------------------


@respx.mock
async def test_the_api_token_never_appears_in_an_error_string():
    """The error is written to the SQLite cache and printed, so this is the
    difference between a transient 401 and a durable plaintext credential."""
    token = "s3cr3t-okta-token-value"
    config = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": token})
    respx.get(f"{USERS_URL}/jdoe").mock(
        side_effect=httpx.HTTPError(f"connection failed using SSWS {token}")
    )

    result = await _plugin(config).fetch("jdoe")

    assert not result.ok
    assert token not in result.error


# --- Configuration reporting ---------------------------------------------------


def test_plugin_declares_the_credentials_it_needs():
    assert set(OktaPlugin.required_credentials) == {"OKTA_ORG_URL", "OKTA_API_TOKEN"}


def test_unconfigured_plugin_reports_itself_unconfigured():
    assert OktaPlugin(PluginConfig({})).configured is False


def test_configured_plugin_reports_itself_configured():
    assert _plugin().configured is True


# --- Mock mode ------------------------------------------------------------------


async def test_mock_mode_works_with_no_credentials_and_no_network():
    """Lets someone try the tool before any token is provisioned."""
    plugin = OktaPlugin(PluginConfig({"ADD_IT_ALL_MOCK_OKTA": "1"}))

    result = await plugin.fetch("jdoe")

    assert result.ok
    assert result.data["status"] == "ACTIVE"
    assert plugin.configured is True


# --- Custom profile attribute: access_blocked ---------------------------------
#
# Not an Okta status -- a custom Universal Directory attribute this org
# defines (Profile Editor label "ACCESS BLOCKED", variable name
# `access_blocked`). It arrives in the same payload `fetch()` already
# retrieves, so surfacing it costs no extra request.
#
# The value is carried through verbatim: whatever Okta returns is what we
# report, with no interpretation of booleans into yes/no.


def _user_with(profile_extra: dict) -> dict:
    payload = _okta_user()
    payload["profile"].update(profile_extra)
    return payload


@respx.mock
async def test_access_blocked_string_value_is_carried_verbatim():
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with({"access_blocked": "ACCESS BLOCKED"}))
    )

    result = await _plugin().fetch("jdoe")

    assert result.data["access_blocked"] == "ACCESS BLOCKED"


@pytest.mark.parametrize("value", [True, False])
@respx.mock
async def test_boolean_values_are_carried_without_translation(value):
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with({"access_blocked": value}))
    )

    result = await _plugin().fetch("jdoe")

    assert result.data["access_blocked"] is value


@respx.mock
async def test_absent_attribute_is_none_not_an_error():
    """The common case: the org defines the attribute but nobody set it."""
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_okta_user()))

    result = await _plugin().fetch("jdoe")

    assert result.ok
    assert result.data["access_blocked"] is None


@respx.mock
async def test_explicit_null_is_none():
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with({"access_blocked": None}))
    )

    result = await _plugin().fetch("jdoe")

    assert result.data["access_blocked"] is None


@respx.mock
async def test_the_attribute_name_is_configurable():
    """Custom UD attribute names are org-specific; don't hardcode ours into
    a plugin that is meant to work against any Okta org."""
    config = PluginConfig(
        {
            "OKTA_ORG_URL": ORG_URL,
            "OKTA_API_TOKEN": "not-a-real-token",
            "OKTA_ACCESS_ATTRIBUTE": "customAccessFlag",
        }
    )
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with({"customAccessFlag": "BLOCKED"}))
    )

    result = await _plugin(config).fetch("jdoe")

    assert result.data["access_blocked"] == "BLOCKED"


@respx.mock
async def test_default_attribute_name_is_ignored_when_overridden():
    """Overriding the name must actually redirect the read, not add to it."""
    config = PluginConfig(
        {
            "OKTA_ORG_URL": ORG_URL,
            "OKTA_API_TOKEN": "not-a-real-token",
            "OKTA_ACCESS_ATTRIBUTE": "customAccessFlag",
        }
    )
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with({"access_blocked": "ACCESS BLOCKED"}))
    )

    result = await _plugin(config).fetch("jdoe")

    assert result.data["access_blocked"] is None


@respx.mock
async def test_other_profile_attributes_are_not_swept_in():
    """Only the configured attribute is read. Okta profiles routinely carry
    manager, employee id, personal phone -- and everything in `data` is
    written to the plaintext SQLite cache."""
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(
            200,
            json=_user_with({"access_blocked": "ACCESS BLOCKED", "personalPhone": "555-0100"}),
        )
    )

    result = await _plugin().fetch("jdoe")

    assert "555-0100" not in str(result.data) + str(result.properties)


async def test_mock_mode_includes_the_attribute():
    plugin = OktaPlugin(PluginConfig({"ADD_IT_ALL_MOCK_OKTA": "1"}))
    result = await plugin.fetch("jdoe")
    assert "access_blocked" in result.data
