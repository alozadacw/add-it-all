"""
Stage 4: Jamf Pro connector -- the authoritative hardware inventory.

Scope distinction worth repeating to users: Okta's `-d` lists its *device
registry* (machines enrolled through Okta Verify / device trust). Jamf is
the actual managed-hardware inventory. The two legitimately disagree -- a
laptop can be in Jamf and unknown to Okta, or the reverse -- and that
disagreement is itself useful. Neither is "wrong".

**Auth is a token exchange, not a static key.** Jamf Pro's current method
is an API Client: POST client_id/client_secret to `/api/oauth/token` and
get a short-lived bearer token back (~20 minutes). The token is fetched
once per run and reused, because a lookup that shows both computers and
mobile devices would otherwise pay for two exchanges.

**`lastContactTime` and `lastReportDate` are different things** and this
module never conflates them. The first is when the device last checked in;
the second is when it last submitted a full inventory. A machine can check
in daily while its inventory is months stale, so reporting either as "last
seen" would answer a question nobody asked. Same class of trap as Okta's
device `lastUpdated`, which is why both are carried under their own names.

All HTTP is mocked. Every credential here is obviously fake.

Run just this stage:  pytest -m jamf
"""

from __future__ import annotations

import httpx
import pytest
import respx
from jamf_plugin.plugin import JamfPlugin, build_user_filter

from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.jamf

BASE = "https://example.jamfcloud.com"
TOKEN_URL = f"{BASE}/api/oauth/token"
COMPUTERS_URL = f"{BASE}/api/v1/computers-inventory"
MOBILE_URL = f"{BASE}/api/v2/mobile-devices/detail"

CONFIG = PluginConfig({
    "JAMF_BASE_URL": BASE,
    "JAMF_CLIENT_ID": "not-a-real-client-id",
    "JAMF_CLIENT_SECRET": "not-a-real-secret",
})


def _token(expires_in: int = 1200) -> dict:
    return {"access_token": "fake-bearer-token", "expires_in": expires_in, "token_type": "Bearer"}


def _computer(
    name="Jane's MacBook Pro",
    serial="C02XYZ123ABC",
    username="jdoe",
    managed=True,
    last_contact="2026-09-20T08:15:00.000Z",
    last_report="2026-09-19T03:00:00.000Z",
):
    return {
        "id": "101",
        "general": {
            "name": name,
            "lastContactTime": last_contact,
            "lastReportDate": last_report,
            "remoteManagement": {"managed": managed},
            "supervised": True,
        },
        "hardware": {
            "serialNumber": serial,
            "model": "MacBook Pro (16-inch, 2021)",
            "modelIdentifier": "MacBookPro18,3",
        },
        "operatingSystem": {"name": "macOS", "version": "15.6.0", "build": "24G84"},
        "userAndLocation": {
            "username": username,
            "realname": "Jane Doe",
            "email": "jdoe@example.com",
            "position": "Engineer",
        },
        "diskEncryption": {"individualRecoveryKeyValidityStatus": "VALID"},
    }


def _page(results, total=None):
    return {"totalCount": total if total is not None else len(results), "results": results}


def _plugin(config: PluginConfig = CONFIG) -> JamfPlugin:
    return JamfPlugin(config)


def _mock_token(**kw):
    return respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_token(**kw)))


# --- Contract ----------------------------------------------------------------


def test_required_credentials_are_declared():
    assert set(JamfPlugin.required_credentials) == {
        "JAMF_BASE_URL", "JAMF_CLIENT_ID", "JAMF_CLIENT_SECRET"
    }


# --- The RSQL filter ----------------------------------------------------------


def test_filter_matches_the_username_field():
    assert 'userAndLocation.username=="jdoe"' == build_user_filter("jdoe")


def test_filter_escapes_embedded_quotes():
    """A quote would otherwise terminate the RSQL string."""
    assert '\\"' in build_user_filter('jd"oe')


def test_filter_trims_surrounding_whitespace():
    assert build_user_filter("  jdoe  ") == build_user_filter("jdoe")


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_identifier_is_rejected(blank):
    with pytest.raises(ValueError):
        build_user_filter(blank)


# --- Auth ---------------------------------------------------------------------


@respx.mock
async def test_the_client_credentials_are_exchanged_for_a_bearer_token():
    route = _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("jdoe")

    body = route.calls.last.request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=not-a-real-client-id" in body


@respx.mock
async def test_the_bearer_token_is_used_on_the_inventory_call():
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("jdoe")

    assert route.calls.last.request.headers["Authorization"] == "Bearer fake-bearer-token"


@respx.mock
async def test_the_token_is_fetched_once_and_reused_across_sections():
    """Showing computers and mobile devices must not pay for two exchanges."""
    token = _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))
    respx.get(MOBILE_URL).mock(return_value=httpx.Response(200, json=_page([])))

    plugin = _plugin()
    await plugin.fetch("jdoe")
    await plugin.fetch_mobile_devices("jdoe")

    assert token.call_count == 1


@respx.mock
async def test_a_rejected_client_credential_is_an_actionable_error():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))

    result = await _plugin().fetch("jdoe")

    assert not result.ok
    assert "client" in result.error.lower() or "credential" in result.error.lower()


@respx.mock
async def test_the_client_secret_never_appears_in_an_error():
    secret = "s3cr3t-jamf-client-secret"
    config = PluginConfig({
        "JAMF_BASE_URL": BASE, "JAMF_CLIENT_ID": "id", "JAMF_CLIENT_SECRET": secret})
    respx.post(TOKEN_URL).mock(side_effect=httpx.HTTPError(f"failed sending {secret}"))

    result = await _plugin(config).fetch("jdoe")

    assert secret not in result.error


# --- Computers ------------------------------------------------------------------


@respx.mock
async def test_computers_are_returned_normalised():
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([_computer()])))

    result = await _plugin().fetch("jdoe")

    assert result.ok
    assert result.data["count"] == 1
    device = result.data["devices"][0]
    assert device["name"] == "Jane's MacBook Pro"
    assert device["serial_number"] == "C02XYZ123ABC"
    assert device["model"] == "MacBook Pro (16-inch, 2021)"
    assert device["os"] == "macOS 15.6.0"
    assert device["managed"] is True


@respx.mock
async def test_check_in_and_inventory_dates_are_kept_separate():
    """A machine can check in daily while its inventory is months stale.
    Reporting either as "last seen" would answer a question nobody asked."""
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([
        _computer(last_contact="2026-09-20T08:15:00.000Z", last_report="2026-06-01T03:00:00.000Z")
    ])))

    device = (await _plugin().fetch("jdoe")).data["devices"][0]

    assert device["last_check_in"] == "2026-09-20"
    assert device["last_inventory"] == "2026-06-01"


@respx.mock
async def test_an_unmanaged_device_is_still_listed():
    """Unmanaged-but-present is a real state, and arguably the interesting
    one -- Jamf knows about the machine but cannot act on it."""
    _mock_token()
    respx.get(COMPUTERS_URL).mock(
        return_value=httpx.Response(200, json=_page([_computer(managed=False)])))

    device = (await _plugin().fetch("jdoe")).data["devices"][0]

    assert device["managed"] is False


@respx.mock
async def test_a_user_with_no_devices_is_a_success():
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    result = await _plugin().fetch("jdoe")

    assert result.ok
    assert result.data["devices"] == []
    assert "no-devices" in result.tags


@respx.mock
async def test_missing_sections_do_not_crash():
    """Jamf omits a section entirely when it has nothing for it."""
    _mock_token()
    respx.get(COMPUTERS_URL).mock(
        return_value=httpx.Response(200, json=_page([{"id": "1", "general": {"name": "Bare"}}])))

    result = await _plugin().fetch("jdoe")

    assert result.ok
    assert result.data["devices"][0]["serial_number"] is None


@respx.mock
async def test_several_devices_are_all_returned():
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([
        _computer(name="MacBook", serial="AAA111"),
        _computer(name="iMac", serial="BBB222"),
    ])))

    result = await _plugin().fetch("jdoe")

    assert {d["serial_number"] for d in result.data["devices"]} == {"AAA111", "BBB222"}


# --- Request shape ----------------------------------------------------------------


@respx.mock
async def test_the_query_is_filtered_to_the_user_server_side():
    """Jamf inventories run to thousands of machines; filtering client-side
    would pull the whole fleet to show one person's laptop."""
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("jdoe")

    assert 'userAndLocation.username=="jdoe"' in route.calls.last.request.url.params["filter"]


@respx.mock
async def test_only_the_sections_we_render_are_requested():
    """A full inventory record carries applications, fonts, plugins, local
    accounts and certificates. Everything in `data` reaches the plaintext
    cache, so unrendered sections are not asked for."""
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("jdoe")

    sections = route.calls.last.request.url.params.multi_items()
    requested = {v for k, v in sections if k == "section"}
    assert "HARDWARE" in requested
    assert "APPLICATIONS" not in requested
    assert "FONTS" not in requested


# --- Failure modes -------------------------------------------------------------------


@respx.mock
async def test_forbidden_on_inventory_is_an_actionable_error():
    """An API Role can authenticate yet lack Read Computers."""
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(403))

    result = await _plugin().fetch("jdoe")

    assert not result.ok
    assert "read" in result.error.lower() or "privilege" in result.error.lower()


@respx.mock
async def test_timeout_is_an_error_result():
    _mock_token()
    respx.get(COMPUTERS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await _plugin().fetch("jdoe")

    assert not result.ok


async def test_missing_credentials_become_an_error_result():
    result = await JamfPlugin(PluginConfig({})).fetch("jdoe")

    assert not result.ok
    assert "JAMF_BASE_URL" in result.error


@respx.mock
async def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    config = PluginConfig({
        "JAMF_BASE_URL": f"{BASE}/", "JAMF_CLIENT_ID": "i", "JAMF_CLIENT_SECRET": "s"})
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin(config).fetch("jdoe")

    assert route.called


# --- Mobile devices ---------------------------------------------------------------


@respx.mock
async def test_mobile_devices_are_a_separate_call():
    """Most lookups want laptops. Phones cost another request, so they are
    opt-in rather than folded into the default view."""
    _mock_token()
    computers = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))
    respx.get(MOBILE_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("jdoe")

    assert computers.called


@respx.mock
async def test_mobile_devices_are_returned_normalised():
    _mock_token()
    respx.get(MOBILE_URL).mock(return_value=httpx.Response(200, json=_page([{
        "mobileDeviceId": "55",
        "name": "Jane's iPhone",
        "serialNumber": "F2LXYZ",
        "model": "iPhone 15 Pro",
        "osVersion": "18.2",
        "managed": True,
        "lastInventoryUpdateDate": "2026-09-18T10:00:00.000Z",
        "userAndLocation": {"username": "jdoe"},
    }])))

    result = await _plugin().fetch_mobile_devices("jdoe")

    assert result.ok
    device = result.data["devices"][0]
    assert device["name"] == "Jane's iPhone"
    assert device["serial_number"] == "F2LXYZ"
    assert device["model"] == "iPhone 15 Pro"


# --- Personal data ------------------------------------------------------------------


@respx.mock
async def test_the_assigned_username_is_carried_but_not_their_contact_details():
    """`userAndLocation` also carries realname, email and position. The
    username is needed to confirm the device is attributed to the right
    person; the rest is not needed to answer "what hardware do they have",
    and `data` reaches the plaintext local cache."""
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([_computer()])))

    result = await _plugin().fetch("jdoe")

    assert result.data["devices"][0]["assigned_to"] == "jdoe"
    blob = repr(result.data)
    assert "jdoe@example.com" not in blob
    assert "Engineer" not in blob


# --- Mock mode -------------------------------------------------------------------------


async def test_mock_mode_works_with_no_credentials():
    plugin = JamfPlugin(PluginConfig({"ADD_IT_ALL_MOCK_JAMF": "1"}))

    result = await plugin.fetch("jdoe")

    assert result.ok
    assert result.data["devices"]
    assert result.data["devices"][0]["serial_number"]


async def test_mock_mode_covers_mobile_devices_too():
    plugin = JamfPlugin(PluginConfig({"ADD_IT_ALL_MOCK_JAMF": "1"}))

    result = await plugin.fetch_mobile_devices("jdoe")

    assert result.ok
    assert result.data["devices"]
