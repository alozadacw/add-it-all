"""
Stage 2: devices assigned to a user in Okta (`lookup-cli okta <user> -d`).

Scope note: this is Okta's device registry (Okta Verify / device trust),
NOT the Jamf hardware inventory that Stage 4 will add. A user can
own a laptop that Okta has never seen.

All HTTP is mocked. Every credential here is obviously fake.

Run just this stage:  pytest -m okta
"""

from __future__ import annotations

import httpx
import pytest
import respx
from okta_plugin.plugin import OktaPlugin

from lookup_cli.plugins.config import PluginConfig

pytestmark = pytest.mark.okta

ORG_URL = "https://acme.okta.com"
USERS_URL = f"{ORG_URL}/api/v1/users"
USER_ID = "00u1abcdefGHIJKLmno7"
DEVICES_URL = f"{USERS_URL}/{USER_ID}/devices"

CONFIG = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "not-a-real-token"})


def _user_payload() -> dict:
    return {
        "id": USER_ID,
        "status": "ACTIVE",
        "profile": {"login": "jdoe", "email": "jdoe@example.com"},
    }


def _device_link(
    serial: str = "C02XYZ123ABC",
    name: str = "Jane's MacBook Pro",
    platform: str = "MACOS",
) -> dict:
    """The shape Okta returns from /users/{id}/devices: a link wrapping a device."""
    return {
        "id": "guo1s5zjrkhcgHFP80g7",
        "created": "2024-05-01T10:00:00.000Z",
        "managementStatus": "MANAGED",
        "screenLockType": "BIOMETRIC",
        "device": {
            "id": "guo1s5zjrkhcgHFP80g7",
            "status": "ACTIVE",
            "lastUpdated": "2026-08-01T10:00:00.000Z",
            "profile": {
                "displayName": name,
                "platform": platform,
                "manufacturer": "Apple",
                "model": "MacBookPro18,3",
                "osVersion": "15.6.0",
                "serialNumber": serial,
                "registered": True,
                "diskEncryptionType": "FULL",
            },
        },
    }


def _plugin(config: PluginConfig = CONFIG) -> OktaPlugin:
    return OktaPlugin(config)


# --- Happy path ---------------------------------------------------------------


@respx.mock
async def test_devices_are_returned_and_normalised():
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[_device_link()]))

    result = await _plugin().fetch_devices("jdoe")

    assert result.ok
    assert result.data["count"] == 1
    device = result.data["devices"][0]
    assert device["display_name"] == "Jane's MacBook Pro"
    assert device["serial_number"] == "C02XYZ123ABC"
    assert device["platform"] == "MACOS"
    assert device["model"] == "MacBookPro18,3"
    assert device["os_version"] == "15.6.0"
    assert device["status"] == "ACTIVE"
    assert device["management_status"] == "MANAGED"


@respx.mock
async def test_multiple_devices_are_all_returned():
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    respx.get(DEVICES_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _device_link(serial="AAA111", name="MacBook"),
                _device_link(serial="BBB222", name="iPhone", platform="IOS"),
            ],
        )
    )

    result = await _plugin().fetch_devices("jdoe")

    assert result.data["count"] == 2
    assert {d["serial_number"] for d in result.data["devices"]} == {"AAA111", "BBB222"}


@respx.mock
async def test_user_with_no_devices_is_a_success_with_an_empty_list():
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))

    result = await _plugin().fetch_devices("jdoe")

    assert result.ok
    assert result.data["count"] == 0
    assert result.data["devices"] == []
    assert "no-devices" in result.tags


@respx.mock
async def test_a_flat_device_payload_is_also_handled():
    """Defensive: not every Okta response nests the device under a link."""
    flat = _device_link()["device"]
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[flat]))

    result = await _plugin().fetch_devices("jdoe")

    assert result.data["devices"][0]["serial_number"] == "C02XYZ123ABC"


@respx.mock
async def test_missing_device_profile_fields_do_not_crash():
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[{"device": {"id": "x"}}]))

    result = await _plugin().fetch_devices("jdoe")

    assert result.ok
    assert result.data["devices"][0]["serial_number"] is None


# --- Pagination ---------------------------------------------------------------


@respx.mock
async def test_paginated_device_lists_are_fully_assembled():
    """Okta paginates with a Link: rel="next" header. Stopping at page one
    would silently under-report someone's devices."""
    page_two = f"{DEVICES_URL}?after=abc"
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    # Register the more specific route first: respx ignores the query string
    # when a pattern has none, so the bare route would otherwise swallow both
    # requests and the test would pass while paging silently did nothing.
    respx.get(DEVICES_URL, params={"after": "abc"}).mock(
        return_value=httpx.Response(200, json=[_device_link(serial="BBB222")])
    )
    respx.get(DEVICES_URL).mock(
        return_value=httpx.Response(
            200,
            json=[_device_link(serial="AAA111")],
            headers={"Link": f'<{page_two}>; rel="next"'},
        )
    )

    result = await _plugin().fetch_devices("jdoe")

    assert result.data["count"] == 2
    assert {d["serial_number"] for d in result.data["devices"]} == {"AAA111", "BBB222"}


@respx.mock
async def test_self_referential_next_link_cannot_loop_forever():
    """A malformed/looping Link header must not hang the CLI."""
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    respx.get(DEVICES_URL).mock(
        return_value=httpx.Response(
            200,
            json=[_device_link()],
            headers={"Link": f'<{DEVICES_URL}>; rel="next"'},
        )
    )

    result = await _plugin().fetch_devices("jdoe")

    assert result.ok  # bounded, not hung


# --- Avoiding a redundant lookup ------------------------------------------------


@respx.mock
async def test_supplying_a_known_okta_id_skips_the_user_lookup():
    """`status -d` already fetched the user; don't pay for it twice."""
    user_route = respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_payload())
    )
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch_devices("jdoe", okta_id=USER_ID)

    assert not user_route.called


# --- Failure modes ---------------------------------------------------------------


@respx.mock
async def test_unknown_user_reports_not_found_rather_than_an_error():
    respx.get(f"{USERS_URL}/ghost").mock(return_value=httpx.Response(404))

    result = await _plugin().fetch_devices("ghost")

    assert result.ok
    assert result.data["found"] is False
    assert result.data["devices"] == []
    assert "not-found" in result.tags


@respx.mock
async def test_devices_endpoint_unavailable_is_an_actionable_error():
    """Okta Classic orgs have no device API -- say so instead of "not found"."""
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(404))

    result = await _plugin().fetch_devices("jdoe")

    assert not result.ok
    assert "device" in result.error.lower()


@respx.mock
async def test_auth_error_on_devices_becomes_an_error_result():
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(403))

    result = await _plugin().fetch_devices("jdoe")

    assert not result.ok


@respx.mock
async def test_timeout_on_devices_becomes_an_error_result():
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    respx.get(DEVICES_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await _plugin().fetch_devices("jdoe")

    assert not result.ok


async def test_missing_credentials_become_an_error_result():
    result = await OktaPlugin(PluginConfig({})).fetch_devices("jdoe")
    assert not result.ok
    assert "OKTA_ORG_URL" in result.error


@respx.mock
async def test_the_api_token_never_appears_in_a_devices_error():
    token = "s3cr3t-okta-token-value"
    config = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": token})
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    respx.get(DEVICES_URL).mock(side_effect=httpx.HTTPError(f"failed using SSWS {token}"))

    result = await _plugin(config).fetch_devices("jdoe")

    assert token not in result.error


# --- Mock mode -------------------------------------------------------------------


async def test_mock_mode_returns_fixture_devices_without_network():
    plugin = OktaPlugin(PluginConfig({"LOOKUP_CLI_MOCK_OKTA": "1"}))

    result = await plugin.fetch_devices("jdoe")

    assert result.ok
    assert result.data["count"] >= 1
    assert result.data["devices"][0]["serial_number"]


# --- The plain lookup stays cheap --------------------------------------------------


@respx.mock
async def test_plain_fetch_does_not_call_the_devices_endpoint():
    """Devices cost an extra round trip, so `fetch()` -- which Stage 7 runs for
    every plugin on every lookup -- must not pay for them."""
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    devices_route = respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch("jdoe")

    assert not devices_route.called
